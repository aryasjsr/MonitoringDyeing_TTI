
import os
import time
import requests
import threading
import json
from pymodbus.client import ModbusTcpClient
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv()
#  Konfigurasi InfluxDB 
INFLUX_URL = os.getenv('INFLUX_URL') 
INFLUX_TOKEN = os.getenv('INFLUX_TOKEN')
INFLUX_ORG = os.getenv('INFLUX_ORG')
INFLUX_BUCKET = os.getenv('INFLUX_BUCKET')
                          
#  Confguration
API_URL_BATCH = os.getenv('API_URL_BATCH')
API_TRIGGER_URL = os.getenv('API_TRIGGER_URL')
API_URL_STRINGS = os.getenv('API_URL_STRINGS')
API_URL_STRINGS_CONF = os.getenv('API_URL_STRINGS_CONF')
CONFIG_FILE = 'machines.json'
API_FETCH_INTERVAL = 10
READ_INTERVAL_SECONDS = 5

#  Variabel 
latest_sensor_data_per_machine = {}
sensor_data_lock = threading.Lock()
latest_hmi_strings_per_machine = {}
hmi_data_lock = threading.Lock()
hmi_write_in_progress = threading.Event()

#  Inisialisasi InfluxDB Client 
influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = influx_client.write_api(write_options=SYNCHRONOUS)

#  Fungsi untuk decode register ke string
def decode_registers_to_string(registers: list, swap_bytes: bool = True) -> str:
    byte_data = bytearray()
    for reg in registers:
        high_byte = (reg >> 8) & 0xff
        low_byte = reg & 0xff
        if swap_bytes:
            byte_data.extend([low_byte, high_byte])
        else:
            byte_data.extend([high_byte, low_byte])
    return byte_data.decode('ascii', errors='ignore').strip('\x00').strip()

#  Fungsi untuk encode string ke register
def encode_string_manually(text: str, swap_bytes: bool = True) -> list[int]:
    if len(text) % 2 != 0: text += ' '
    payload = []
    for i in range(0, len(text), 2):
        char1, char2 = text[i], text[i+1]
        byte1, byte2 = ord(char1), ord(char2)
        register_value = (byte2 << 8) | byte1 if swap_bytes else (byte1 << 8) | byte2
        payload.append(register_value)
    return payload

#  THREAD 1: Pembaca data hmi dan kirim ke InfluxDB
def machine_monitoring_thread(machine_config: dict):
    no_mc = machine_config['noMc']
    ip = machine_config['ip_address']
    port = machine_config['port']
    regs = machine_config['read_registers']
    client = ModbusTcpClient(ip, port=port)
    
    previous_values = {}

    print(f"[MC-{no_mc}] Thread monitoring dimulai.")
    while True:
        if hmi_write_in_progress.is_set():
            print(f"[Sensor Reader MC-{no_mc}] Proses tulis sedang berjalan, pembacaan dijeda.")
            time.sleep(READ_INTERVAL_SECONDS)
            continue
        try:
            client.connect()
            
            current_values = {}
            
            # 1. Baca semua register single-word
            for name, address in regs.items():
                if name != 'batch': # Lewati register batch untuk dibaca terpisah
                    response = client.read_holding_registers(address, count=1, slave=1)
                    if response.isError(): raise ConnectionError(f"Gagal membaca register '{name}'")
                    current_values[name] = response.registers[0]

            # 2. Baca 7 register untuk batch secara khusus
            batch_address = regs['batch']
            batch_response = client.read_holding_registers(batch_address, count=7, slave=1)
            if batch_response.isError(): raise ConnectionError("Gagal membaca register 'batch'")
            # 3. Konversi 7 register tersebut menjadi satu string
            batch_string = decode_registers_to_string(batch_response.registers)
            current_values['batch'] = batch_string # Simpan string, bukan angka

     
            
            # 1. Data Frekuensi Tinggi (Temp & Seam)
            high_freq_fields = ["temp1", "temp2", "seam_left", "seam_right"]
            point_hf = Point("high_frequency_data").tag("machine_id", no_mc)
            has_new_hf_data = False
            is_machine_on = current_values.get("machine_on", 0) > 0
            was_machine_on = previous_values.get("machine_on", 0) > 0
            for field in high_freq_fields:
                if current_values.get(field) != previous_values.get(field) or (is_machine_on and not was_machine_on):
                    # Bagi dengan 10 untuk mendapatkan nilai desimal
                    value = float(current_values.get(field, 0)) / 10.0 if "temp" in field else float(current_values.get(field, 0))
                    point_hf.field(field, value)
                    has_new_hf_data = True
            if has_new_hf_data:
                write_api.write(bucket=INFLUX_BUCKET, record=point_hf)
                print(f"[MC-{no_mc}] Perubahan data frekuensi tinggi terdeteksi dan dikirim.")

            # 2. Data Frekuensi medium
            medium_freq_fields = ["level", "process", "pattern", "step", "ph", "lit_mpump_hr", "bear_mpump_hr", "seal_mpump_hr", "oil_mpump_hr", "lit_dReelR_hr", "bear_dReelR_hr", "seal_dReelR_hr", "cal_temp1_hr", "cal_temp2_hr","machine_on"]
            point_mf = Point("medium_frequency_data").tag("machine_id", no_mc)
            has_new_mf_data = False
            for field in medium_freq_fields:
                if current_values.get(field) != previous_values.get(field):
                    value = float(current_values.get(field, 0)) / 10.0 if "ph" in field else float(current_values.get(field, 0))
                    point_mf.field(field, value)
                    has_new_mf_data = True
            if has_new_mf_data:
                write_api.write(bucket=INFLUX_BUCKET, record=point_mf)
                print(f"[MC-{no_mc}] Perubahan data frekuensi sedang terdeteksi dan dikirim.")

            # 3. Data Konteks Siklus (Batch, NIK OP, dll.)
            is_machine_on = current_values.get("machine_on", 0) > 0
            was_machine_on = previous_values.get("machine_on", 0) > 0
            
            context_fields = ["nik_op", "batch", "celup", "shift"]
            context_changed = any(current_values.get(f) != previous_values.get(f) for f in context_fields)
            context_changed_ket = current_values.get("ket_mesin_off", 0) != previous_values.get("ket_mesin_off", 0)

            if (is_machine_on and not was_machine_on) or (is_machine_on and context_changed):
                point_context = Point("cycle_context_data").tag("machine_id", no_mc)
                for field in context_fields:
                    # Kirim sebagai tipe data yang benar (string atau integer)
                    value = current_values.get(field, 0)
                    point_context.field(field, str(value) if isinstance(value, str) else int(value))
                write_api.write(bucket=INFLUX_BUCKET, record=point_context)
                print(f"[MC-{no_mc}] Data konteks siklus (awal/perubahan) dikirim.")
                
            #  Jika mesin baru saja dimatikan, kirim keterangan mesin off
            elif (not is_machine_on and was_machine_on) or (context_changed_ket and not is_machine_on):
                point_context = Point("cycle_context_data").tag("machine_id", no_mc)
                ket_mesin_off = current_values.get("ket_mesin_off", 0)
                point_context.field("ket_mesin_off", ket_mesin_off)
                write_api.write(bucket=INFLUX_BUCKET, record=point_context)
                print(f"[MC-{no_mc}] Data konteks keterangan mesin off dikirim.")

            # 4. Data (Maintenance)
            current_reset = current_values.get("id_reset", 0)
            previous_reset = previous_values.get("id_reset", 0)
            
            if current_reset != previous_reset and current_reset > 0:
                point_maint = Point("maintenance_events").tag("machine_id", no_mc)
                point_maint.field("nik_maintanance", str(current_values.get("nik_maintanance", "")))
                point_maint.field("id_reset", current_values.get("id_reset", 0))
                write_api.write(bucket=INFLUX_BUCKET, record=point_maint)
                print(f"[MC-{no_mc}] Pemicu reset terdeteksi, data maintenance dikirim.")

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

            if previous_process != 305 and current_process == 305:
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


            previous_values = current_values.copy()

        except Exception as e:
            print(f"[MC-{no_mc}] Terjadi error: {e}")
        finally:
            if client.is_socket_open(): client.close()
            time.sleep(READ_INTERVAL_SECONDS)


#  THREAD 3: Pengambil Data String dari API 
def api_hmi_reader_thread():
    global latest_hmi_strings_per_machine
    print("[API HMI Reader] Thread dimulai.")
    while True:
        try:
            response = requests.get(API_URL_STRINGS, timeout=5)
            if response.status_code == 200:
                response_data = response.json()
                if response_data.get("status"):
                    all_machines_data = response_data.get("data", {})
                    with hmi_data_lock:
                        for mc_id_str, machine_data in all_machines_data.items():
                            mc_id_int = int(mc_id_str)
                            latest_hmi_strings_per_machine[mc_id_int] = machine_data
        except Exception as e:
            print(f"[API HMI Reader] Gagal mengambil data string: {e}")
        time.sleep(API_FETCH_INTERVAL)

# THREAD 4: Penulis Data ke HMI 
def hmi_writer_thread(machine_config: dict):
    global latest_hmi_strings_per_machine
    no_mc = machine_config['noMc']
    ip = machine_config['ip_address']
    port = machine_config['port']
    write_regs = machine_config['write_registers']
    client = ModbusTcpClient(ip, port=port)
    
    print(f"[HMI Writer MC-{no_mc}] Thread dimulai.")
    while True:
        data_to_write = None
        with hmi_data_lock:
            if no_mc in latest_hmi_strings_per_machine:
                data_to_write = latest_hmi_strings_per_machine.pop(no_mc)
        
        if data_to_write and data_to_write.get("status"):
            hmi_write_in_progress.set()
            write_successful = False
            try:
                client.connect()
                print(f"[HMI Writer MC-{no_mc}] Data baru terdeteksi, memproses untuk HMI")
                
                batches_written_count = 0
                status_register_addresses = write_regs.get('status_registers', [])
                
                if len(status_register_addresses) != 7:
                    raise ValueError("Konfigurasi 'status_registers' di machines.json harus berisi 7 alamat.")
                for i in range(1, 8):
                    batch_key = f"batch{i}"
                    if batch_key in data_to_write and data_to_write[batch_key]:
                        status_address = status_register_addresses[i-1]
                        data_address = write_regs['batch_map'][batch_key]
                        string_value = str(data_to_write[batch_key]).ljust(14)
                    
                        payload = encode_string_manually(string_value)
                        print(f"  Menulis {batch_key} ('{string_value}') ke alamat {data_address}")
                        client.write_registers(data_address, payload, slave=1)
                        print(f"Mengatur status ON (1) untuk {batch_key} di alamat {status_address}")
                        client.write_register(status_address, 1, slave=1)
                        batches_written_count += 1

                if batches_written_count > 0:
                    print(f"[HMI Writer MC-{no_mc}] {batches_written_count} batch berhasil ditulis.")
                    write_successful = True 
                else:
                    print(f"[HMI Writer MC-{no_mc}] Status True, tetapi tidak ada data batch valid untuk ditulis.")
                    write_successful = True 

            except Exception as e:
                print(f"[HMI Writer MC-{no_mc}] Gagal menulis ke HMI: {e}")
            finally:
                if client.is_socket_open(): client.close()
                hmi_write_in_progress.clear()

            if write_successful:
                try:
                    URL = f"{API_URL_STRINGS_CONF}/{no_mc}"
                    print(f"[HMI Writer MC-{no_mc}] Mengirim konfirmasi ke {URL}...")
                    requests.post(URL, timeout=10)
                    print(f"[HMI Writer MC-{no_mc}] Konfirmasi berhasil dikirim.")
                except Exception as e:
                    print(f"[HMI Writer MC-{no_mc}] Gagal mengirim konfirmasi: {e}")
        
        time.sleep(1)


if __name__ == "__main__":
    print(" Modbus Multi-Master READ-WRITE Start")
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            all_machines = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: File konfigurasi '{CONFIG_FILE}' not found!")
        exit()

    
    api_hmi_reader = threading.Thread(target=api_hmi_reader_thread, daemon=True)
    api_hmi_reader.start()

    for machine_conf in all_machines:
        reader_sensor = threading.Thread(target=machine_monitoring_thread, args=(machine_conf,), daemon=True)
        writer = threading.Thread(target=hmi_writer_thread, args=(machine_conf,), daemon=True)
        reader_sensor.start()
        writer.start()

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        print("\nProgram dihentikan.")