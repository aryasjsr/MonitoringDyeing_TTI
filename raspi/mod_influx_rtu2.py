import os
import time
import requests
import threading
import json
from dotenv import load_dotenv

from pymodbus.client import ModbusSerialClient
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv()

# =========================
# Konfigurasi InfluxDB
# =========================
INFLUX_URL   = os.getenv('INFLUX_URL')
INFLUX_TOKEN = os.getenv('INFLUX_TOKEN')
INFLUX_ORG   = os.getenv('INFLUX_ORG')
INFLUX_BUCKET= os.getenv('INFLUX_BUCKET')

# =========================
# Konfigurasi API
# =========================
API_URL_BATCH      = os.getenv('API_URL_BATCH')       # POST batch finish payload
API_TRIGGER_URL    = os.getenv('API_TRIGGER_URL')     # POST trigger after finish
API_URL_STRINGS    = os.getenv('API_URL_STRINGS')     # GET HMI strings (semua mesin)
API_URL_STRINGS_CONF = os.getenv('API_URL_STRINGS_CONF')  # POST confirm /{no_mc}

# =========================
# Konfigurasi Serial Modbus
# =========================
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
BAUDRATE    = int(os.getenv("BAUDRATE", "9600"))
CONFIG_FILE = 'machines2.json'

# =========================
# Interval
# =========================
API_FETCH_INTERVAL   = 10   # detik
READ_INTERVAL_SECONDS= 5    # detik
SMALL_READ_GAP       = 0.01 # jeda kecil antar request di bus

# =========================
# Variabel global
# =========================
latest_hmi_strings_per_machine = {}
hmi_data_lock = threading.Lock()
hmi_write_in_progress = threading.Event()  # optional; reader akan tetap jalan, tapi bisa dipakai untuk jeda kalau diinginkan

# Satu client Modbus untuk semua thread + satu lock bus
client = ModbusSerialClient(
    port=SERIAL_PORT,
    baudrate=BAUDRATE,
    parity="E",
    stopbits=1,
    bytesize=8,
    timeout=1.0,     
    retries=3,     
)
bus_lock = threading.Lock()

# InfluxDB
influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = influx_client.write_api(write_options=SYNCHRONOUS)


# =========================
# Util: String <-> Register
# =========================
def decode_registers_to_string(registers: list, swap_bytes: bool = True) -> str:
    byte_data = bytearray()
    for reg in registers:
        high = (reg >> 8) & 0xFF
        low  = reg & 0xFF
        if swap_bytes:
            byte_data.extend([low, high])
        else:
            byte_data.extend([high, low])
    return byte_data.decode('ascii', errors='ignore').strip('\x00').strip()

def encode_string_manually(text: str, swap_bytes: bool = True) -> list[int]:
    if len(text) % 2 != 0:
        text += ' '
    payload = []
    for i in range(0, len(text), 2):
        b1 = ord(text[i])
        b2 = ord(text[i+1])
        reg = (b2 << 8) | b1 if swap_bytes else (b1 << 8) | b2
        payload.append(reg)
    return payload


# =========================
# THREAD 1: Reader per mesin
# =========================
def machine_monitoring_thread(machine_config: dict):
    no_mc   = machine_config['noMc']
    unit_id = machine_config['slave_id']
    regs    = machine_config['read_registers']

    previous_values = {}
    print(f"[MC-{no_mc}] Thread monitoring dimulai (slave {unit_id}).")

    while True:
        try:
            current_values = {}

            # --- Baca semua register single-word (kecuali 'batch')
            for name, address in regs.items():
                if name == 'batch':
                    continue
                with bus_lock:
                    resp = client.read_holding_registers(address, count=1, slave=unit_id)
                if hasattr(resp, "isError") and resp.isError():
                    raise ConnectionError(f"Gagal membaca register '{name}' @ {address} (MC-{no_mc})")
                current_values[name] = resp.registers[0]
                time.sleep(SMALL_READ_GAP)

            # --- Baca 'batch' 7 register -> 14 chars
            with bus_lock:
                batch_resp = client.read_holding_registers(regs['batch'], count=7, slave=unit_id)
            if hasattr(batch_resp, "isError") and batch_resp.isError():
                raise ConnectionError(f"Gagal membaca 'batch' @ {regs['batch']} (MC-{no_mc})")
            current_values['batch'] = decode_registers_to_string(batch_resp.registers)

            # ---------- High frequency (temp/seam)
            high_fields = ["temp1", "temp2", "seam_left", "seam_right"]
            point_hf = Point("high_frequency_data").tag("machine_id", no_mc)
            changed_hf = False
            is_on  = current_values.get("machine_on", 0) > 0
            was_on = previous_values.get("machine_on", 0) > 0
            for f in high_fields:
                if current_values.get(f) != previous_values.get(f) or (is_on and not was_on):
                    val_raw = current_values.get(f, 0)
                    if "temp" in f:
                        val = float(val_raw) / 10.0
                    else:
                        val = float(val_raw)
                    point_hf.field(f, val)
                    changed_hf = True
            if changed_hf:
                write_api.write(bucket=INFLUX_BUCKET, record=point_hf)
                print(f"[MC-{no_mc}] HF changed -> write Influx")

            # ---------- Medium frequency
            medium_fields = [
                "level","process","pattern","step","ph",
                "lit_mpump_hr","bear_mpump_hr","seal_mpump_hr","oil_mpump_hr",
                "lit_dReelR_hr","bear_dReelR_hr","seal_dReelR_hr",
                "cal_temp1_hr","cal_temp2_hr","machine_on","lit_dReelL_hr","bear_dReelL_hr","seal_dReelL_hr"
            ]
            point_mf = Point("medium_frequency_data").tag("machine_id", no_mc)
            changed_mf = False
            for f in medium_fields:
                if current_values.get(f) != previous_values.get(f):
                    val_raw = current_values.get(f, 0)
                    if f == "ph":
                        val = float(val_raw) / 10.0
                    else:
                        val = float(val_raw)
                    point_mf.field(f, val)
                    changed_mf = True
            if changed_mf:
                write_api.write(bucket=INFLUX_BUCKET, record=point_mf)
                print(f"[MC-{no_mc}] MF changed -> write Influx")

            # ---------- CycleContext (on-change saat mesin ON ->OFF atau OFF->ON)
            is_on  = current_values.get("machine_on", 0) > 0
            was_on = previous_values.get("machine_on", 0) > 0
            ctx_fields = ["nik_op", "batch", "celup", "shift"]
            ctx_changed = any(current_values.get(f) != previous_values.get(f) for f in ctx_fields)
            if (is_on and not was_on) or (is_on and ctx_changed):
                point_ctx = Point("cycle_context_data").tag("machine_id", no_mc)
                for f in ctx_fields:
                    v = current_values.get(f, "")
                    if isinstance(v, str):
                        point_ctx.field(f, v)
                    else:
                        point_ctx.field(f, int(v))
                write_api.write(bucket=INFLUX_BUCKET, record=point_ctx)
                print(f"[MC-{no_mc}] Context changed -> write Influx")

            # ---------- Maintenance reset 
            cur_reset  = current_values.get("id_reset", 0)
            prev_reset = previous_values.get("id_reset", 0)
            if cur_reset != prev_reset and cur_reset > 0:
                point_maint = Point("maintenance_events").tag("machine_id", no_mc)
                point_maint.field("nik_maintanance", str(current_values.get("nik_maintanance", "")))
                point_maint.field("id_reset", int(cur_reset))
                write_api.write(bucket=INFLUX_BUCKET, record=point_maint)
                print(f"[MC-{no_mc}] Maintenance reset event -> write Influx")

            # --- 5. Simpan Batch saat process FINISH ke SQL SERVER lewat API ---
            current_process  = int((current_values or {}).get("process", 0) or 0)
            previous_process = int((previous_values or {}).get("process", 0) or 0)

            def pick_batch(cur: dict | None, prev: dict | None) -> str:
                """Prioritaskan batch dari current, fallback ke previous, else kosong."""
                for src in (cur or {}, prev or {}):
                    try:
                        v = src.get("batch", "")
                        s = "" if v is None else str(v).strip()
                        if s:
                            return s
                    except Exception:
                        # Kalau ada key aneh/tipe tak terduga, lewati
                        continue
                return ""  # dua-duanya kosong

            if previous_process != 355 and current_process == 355:
                batch_name = pick_batch(current_values, previous_values)
                batch_send = {"batch": batch_name}

                try:
                    response = requests.post(API_URL_BATCH, json=batch_send, timeout=10)
                    print(f"[Sender] Mengirim data batch ke API: {batch_send}")
                    print(response.status_code)
                except requests.exceptions.RequestException as e:
                    print(f"[Sender] Tidak dapat terhubung ke API: {e}")

                try:
                    requests.post(API_TRIGGER_URL, timeout=10)
                    print(f"[MC-{no_mc}] Pemicu akhir proses berhasil dikirim ke API 2.")
                except Exception as e:
                    print(f"[MC-{no_mc}] Gagal mengirim pemicu ke API 2: {e}")

            previous_values = current_values

        except Exception as e:
            print(f"[MC-{no_mc}] ERROR: {e}")

        finally:
            time.sleep(READ_INTERVAL_SECONDS)


# ====================================
# THREAD 2: Pengambil Data String via API
# ====================================
def api_hmi_reader_thread():
    global latest_hmi_strings_per_machine
    print("[API HMI Reader] Thread dimulai.")
    while True:
        try:
            resp = requests.get(API_URL_STRINGS, timeout=10)
            resp.raise_for_status()
            response_data = resp.json()
            if response_data.get("status"):
                all_machines_data = response_data.get("data", {})
                with hmi_data_lock:
                    for mc_id_str, machine_data in all_machines_data.items():
                        try:
                            mc_id_int = int(mc_id_str)
                        except ValueError:
                            continue
                        latest_hmi_strings_per_machine[mc_id_int] = machine_data
        except Exception as e:
            print(f"[API HMI Reader] Gagal mengambil data string: {e}")
        time.sleep(API_FETCH_INTERVAL)


# =========================
# THREAD 3: HMI Writer
# =========================
def hmi_writer_thread(machine_config: dict):
    global latest_hmi_strings_per_machine
    no_mc   = machine_config['noMc']
    unit_id = machine_config['slave_id']
    write_regs = machine_config['write_registers']

    print(f"[HMI Writer MC-{no_mc}] Thread dimulai.")
    while True:
        data_to_write = None
        with hmi_data_lock:
            if no_mc in latest_hmi_strings_per_machine:
                data_to_write = latest_hmi_strings_per_machine.pop(no_mc)

        if data_to_write and data_to_write.get("status"):
            
            hmi_write_in_progress.set()
            batches_written_count = 0
            try:
                status_register_addresses = write_regs.get('status_registers', [])
                if len(status_register_addresses) != 7:
                    raise ValueError("Konfigurasi 'status_registers' harus 7 alamat (status batch1..batch7).")

                for i in range(1, 8):
                    key = f"batch{i}"
                    val = data_to_write.get(key)
                    if val:
                        s = str(val).ljust(14)  # 7 reg x 2 char
                        payload = encode_string_manually(s)

                        data_addr = write_regs['batch_map'][key]
                        stat_addr = status_register_addresses[i-1]

                        with bus_lock:
                            # tulis string
                            client.write_registers(data_addr, payload, slave=unit_id)
                            # set status ON
                            client.write_register(stat_addr, 1, slave=unit_id)

                        batches_written_count += 1

                if batches_written_count > 0:
                    print(f"[HMI Writer MC-{no_mc}] {batches_written_count} batch berhasil ditulis.")
                else:
                    print(f"[HMI Writer MC-{no_mc}] Status True, tapi tidak ada batch valid.")

                # Kirim konfirmasi ke API
                try:
                    url = f"{API_URL_STRINGS_CONF}/{no_mc}"
                    r = requests.post(url, timeout=10)
                    r.raise_for_status()
                    print(f"[HMI Writer MC-{no_mc}] Konfirmasi sukses -> {url}")
                except Exception as e:
                    print(f"[HMI Writer MC-{no_mc}] Gagal kirim konfirmasi: {e}")

            except Exception as e:
                print(f"[HMI Writer MC-{no_mc}] ERROR tulis HMI: {e}")
            finally:
                hmi_write_in_progress.clear()

        time.sleep(1)


# =========================
# MAIN
# =========================
if __name__ == "__main__":
    print("Modbus Multi-Slave RS-485: Reader + Writer start")

    # Load konfigurasi mesin
    try:
        with open(CONFIG_FILE, 'r') as f:
            all_machines = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: File konfigurasi '{CONFIG_FILE}' tidak ditemukan!")
        exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: JSON '{CONFIG_FILE}' tidak valid: {e}")
        exit(1)

    # Connect sekali ke port serial
    if not client.connect():
        print("Gagal connect ke port RS-485")
        exit(1)

    # Start thread API reader (ambil batch strings)
    t_api = threading.Thread(target=api_hmi_reader_thread, daemon=True)
    t_api.start()

    # Start thread per mesin: reader + writer
    for mc in all_machines:
        t_reader = threading.Thread(target=machine_monitoring_thread, args=(mc,), daemon=True)
        t_writer = threading.Thread(target=hmi_writer_thread, args=(mc,), daemon=True)
        t_reader.start()
        t_writer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        try:
            client.close()
        except Exception:
            pass
