


# Monitoring Mesin Dyeing TTI

Sistem ini adalah layanan monitoring berbasis Python yang dirancang untuk mengumpulkan data dari mesin *dyeing* (pencelupan). Proyek ini mampu membaca data dari HMI/PLC mesin melalui protokol Modbus (TCP dan RTU), mengirimkan data telemetri ke database InfluxDB, dan melakukan komunikasi dua arah dengan API eksternal untuk sinkronisasi data *batch*.

---

## ⚙️ Fitur Utama

- **Konektivitas Ganda**: Mendukung koneksi Modbus TCP/IP (via Ethernet/IP) dan Modbus RTU (via Serial RS-485).  
- **Pencatatan Data (Data Logging)**: Mengirimkan data sensor (suhu, level, pH, status, dll.) ke InfluxDB untuk monitoring dan analisis deret waktu (*time-series*).  
- **Integrasi API Dua Arah**:  
  1. **Lapor Selesai** — otomatis mengirim nama *batch* ke API eksternal ketika mesin melaporkan status *proses selesai*.  
  2. **Ambil Data Baru** — secara berkala mengambil daftar *batch* baru dari API.  
- **Penulisan HMI (HMI Write-back)**: Menulis data *batch* baru yang diterima dari API kembali ke register HMI mesin.  
- **Multithreading**: Menggunakan *thread* terpisah untuk setiap mesin dan untuk setiap tugas (membaca sensor, menulis ke HMI, mengambil data API), memungkinkan monitoring beberapa mesin secara paralel dan efisien.  

---

## 🔄 Alur Kerja

Sistem ini beroperasi dengan dua alur utama yang berjalan bersamaan:

### 1. Alur Pembacaan (Sensor → InfluxDB & API)
1. Sebuah *thread* (`machine_monitoring_thread`) dibuat untuk setiap mesin dari konfigurasi.
2. *Thread* membaca register HMI (`read_registers`) setiap 5 detik (dapat dikonfigurasi).
3. Data dibandingkan dengan data sebelumnya — jika berubah, dikirim ke InfluxDB dan dikategorikan sebagai:
   - `high_frequency_data` (misal: suhu, seam)
   - `medium_frequency_data` (misal: level, pH, status)
   - `cycle_context_data` (misal: batch, NIK operator, shift)
   - `maintenance_events` (jika terdeteksi pemicu reset)
4. **Pemicu Proses Selesai**: Ketika register `process` bernilai 305 (TCP) atau 355 (RTU), skrip mengambil nama *batch* aktif dan mengirimkannya via `requests.post` ke `API_URL_BATCH` dan `API_TRIGGER_URL`.

### 2. Alur Penulisan (API → HMI)
1. Satu *thread* global (`api_hmi_reader_thread`) melakukan `requests.get` ke `API_URL_STRINGS` setiap 10 detik.
2. API mengembalikan data JSON berisi daftar *batch* baru tiap mesin → disimpan di variabel global.
3. *Thread* `hmi_writer_thread` tiap mesin memantau variabel ini.
4. Jika ada data baru:
   - Mengubah string *batch* (contoh: `"BATCH123"`) ke array integer 16-bit.
   - Menulis ke alamat HMI (`write_registers`).
   - Menulis nilai `1` ke register status *batch*.
   - Mengirim konfirmasi `requests.post` ke `API_URL_STRINGS_CONF`.

---

## 🗂️ Struktur File

| File | Fungsi |
|------|---------|
| `mod_influx.py` | Skrip utama Modbus TCP/IP menggunakan `ModbusTcpClient`. |
| `mod_influx_rtu2.py` | Skrip utama Modbus RTU menggunakan `ModbusSerialClient`, mendukung multi-slave via `bus_lock`. |
| `mod_influx_rtu.py` | Versi lama RTU, tanpa logika “proses selesai ke API”. Gunakan `mod_influx_rtu2.py`. |
| `machines.json` | Konfigurasi mesin untuk `mod_influx.py` (TCP). |
| `machines2.json` | Konfigurasi mesin untuk `mod_influx_rtu2.py` (RTU). |
| `requirements.txt` | Daftar dependensi Python (`pymodbus`, `influxdb-client`, `requests`, dll). |

---

## 🚀 Instalasi dan Penggunaan

### 1. Persiapan Lingkungan
```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS
.\venv\Scripts\activate   # Windows
pip install -r requirements.txt
````

### 2. Konfigurasi `.env`

Buat file `.env` di direktori proyek:

```ini
# InfluxDB
INFLUX_URL=http://localhost:8086
INFLUX_TOKEN=your_influx_token
INFLUX_ORG=your_organization
INFLUX_BUCKET=your_bucket_name

# API
API_URL_BATCH=http://api.example.com/batch/finish
API_TRIGGER_URL=http://api.example.com/trigger
API_URL_STRINGS=http://api.example.com/strings/all
API_URL_STRINGS_CONF=http://api.example.com/strings/confirm

# Hanya untuk Mode RTU
SERIAL_PORT=/dev/ttyUSB0
BAUDRATE=9600
```

### 3. Konfigurasi Mesin

* **TCP:** Edit `machines.json` → sesuaikan `ip_address`, `port`, dan register.
* **RTU:** Edit `machines2.json` → sesuaikan `slave_id` dan register.

### 4. Jalankan Skrip

**Modbus TCP/IP:**

```bash
python mod_influx.py
```

**Modbus RTU (Serial):**

```bash
python mod_influx_rtu2.py
```

---

## 🧠 Catatan Tambahan

* Gunakan `mod_influx_rtu2.py` untuk bus RS-485 dengan banyak mesin (multi-slave).
* Pastikan `bus_lock` aktif untuk mencegah *data collision* antar *thread*.
* Jika komunikasi tidak stabil, periksa `parity`, `stopbits`, dan `baudrate` pada konfigurasi RTU.
* Pastikan alamat register HMI sesuai dengan *mapping* di proyek Anda.
* Pastikan InfluxDB dan API dapat diakses dari jaringan lokal mini-PC atau Raspberry Pi.

---

## 📄 Lisensi

Proyek ini bersifat **internal deployment** dan tidak dimaksudkan untuk publikasi terbuka.

---

## ✉️ Kontak

Untuk dukungan teknis, hubungi **Tim Otomasi & Digital Factory - PT Trisula Textile Industries**.

```

