# Penjelasan Query Grafana - Dashboard Mesin Dyeing 

Dashboard ini menampilkan **9 panel** untuk monitoring real-time Mesin Dyeing , mengambil data dari database InfluxDB dengan menggunakan bahasa query **Flux**.

---

## 📊 Panel Dashboard

| No | Nama Panel | Fungsi |
|---|---|---|
| 1 | **SHIFT** | Menampilkan shift kerja (A/B/C) |
| 2 | **OPERATOR** | Menampilkan nama operator berdasarkan NIK |
| 3 | **JENIS CELUP** | Jenis proses celup (FRESH, REDYE, TOPPING, dll) |
| 4 | **STATUS** | Status mesin (ON/OFF dengan keterangan alasan) |
| 5 | **PROCESS** | Tabel log proses yang sedang/pernah berjalan |
| 6 | **BATCH** | Nama batch yang sedang diproses |
| 7 | **pH** | Nilai pH terakhir dari sensor |
| 8 | **Tabel Status OFF** | Riwayat waktu mesin OFF dengan durasi dan keterangan |
| 9 | **Grafik Time-Series** | Grafik suhu (Temp1, Temp2), level air, dan seam |

---

## 🔍 Penjelasan Query per Panel

### 1️⃣ Panel SHIFT
**Fungsi:** Menampilkan shift kerja saat ini (A, B, atau C)

**Query:**
```flux
from(bucket: "OtomasiEng")
  |> range(start: -1d)
  |> filter(fn: (r) => r["_measurement"] == "cycle_context_data")
  |> filter(fn: (r) => r["machine_id"] == "1")
  |> filter(fn: (r) => r["_field"] == "shift")
  |> last()
  |> distinct(column: "_value")
```

**Cara Kerja:**
1. Mengambil data dari bucket `OtomasiEng` dalam 1 hari terakhir
2. Filter data dengan measurement `cycle_context_data` untuk mesin ID `1`
3. Filter hanya field `shift`
4. Ambil nilai terakhir (`last()`)
5. Tampilkan nilai unik (`distinct`)

**Mapping Nilai:**
- `0` → Shift A
- `1` → Shift B
- `2` → Shift C

---

### 2️⃣ Panel OPERATOR
**Fungsi:** Menampilkan nama operator yang sedang bertugas

**Query:**
```flux
import "influxdata/influxdb/schema"

// Ambil NIK operator terakhir dari mesin
nik_data = from(bucket: "OtomasiEng")
  |> range(start: -1d)
  |> filter(fn: (r) => r["_measurement"] == "cycle_context_data")
  |> filter(fn: (r) => r["_field"] == "nik_op")
  |> filter(fn: (r) => r["machine_id"] == "1")
  |> last() 
  |> rename(columns: {"_value": "nik_op_val"})

// Ambil semua data operator dari database referensi
operator_data = from(bucket: "operatorDF")
  |> range(start: 0)
  |> filter(fn: (r) => r["_measurement"] == "operator_data")
  |> filter(fn: (r) => r["_field"] == "nik_op")
  |> rename(columns: {"_value": "nik_op_val", "name": "operator_name"})

// JOIN NIK untuk mendapatkan nama operator
join(
    tables: {nik: nik_data, op: operator_data},
    on: ["nik_op_val"],
    method: "inner"
)
|> keep(columns: ["_time", "operator_name"])
|> rename(columns: {operator_name: "_value"})
```

**Cara Kerja:**
1. Ambil NIK operator terakhir yang login di mesin 1
2. Ambil database operator lengkap dari bucket `operatorDF`
3. JOIN kedua data berdasarkan NIK yang sama
4. Tampilkan nama operator

---

### 3️⃣ Panel JENIS CELUP
**Fungsi:** Menampilkan jenis proses celup yang sedang berjalan

**Query:**
```flux
from(bucket: "OtomasiEng")
  |> range(start: -1d)
  |> filter(fn: (r) => r["_measurement"] == "cycle_context_data")
  |> filter(fn: (r) => r["machine_id"] == "1")
  |> filter(fn: (r) => r["_field"] == "celup")
  |> last()
  |> distinct(column: "_value")
```

**Mapping Nilai:**
- `0` → FRESH
- `1` → TR FRESH OBS
- `2` → TR FRESH STEP 1
- `3` → TR FRESH STEP 2
- `4` → REDYE
- `5` → TOPPING
- `6` → CUCI
- `7` → CUCI PANCINGAN
- `8` → CUCI MESIN
- `9` → DOUBLE SCOURING

---

### 4️⃣ Panel STATUS
**Fungsi:** Menampilkan status mesin (ON/OFF) dengan keterangan alasan OFF

**Query:**
```flux
import "array"

// Helper function untuk mengambil nilai terakhir dengan fallback 0
getLast = (measurement, fieldName, machineId) => {
  defRow = array.from(rows: [{
      _time: v.timeRangeStart,
      _value: 0.0,
      machine_id: machineId
    }])
    |> toFloat()

  data = from(bucket: "OtomasiEng")
      |> range(start: -1d)
      |> filter(fn: (r) => r._measurement == measurement)
      |> filter(fn: (r) => r.machine_id == machineId)
      |> filter(fn: (r) => r._field == fieldName)
      |> toFloat()

  return union(tables: [defRow, data])
      |> last()
      |> toInt()
}

// Ambil status machine_on
lastOn = getLast(
    measurement: "medium_frequency_data", 
    fieldName: "machine_on", 
    machineId: "1"
  )

// Ambil keterangan mesin OFF
lastOff = getLast(
    measurement: "cycle_context_data", 
    fieldName: "ket_mesin_off", 
    machineId: "1"
  )

// JOIN dan tentukan status akhir
join(tables: {on: lastOn, off: lastOff}, on: ["machine_id"])
  |> map(fn: (r) => ({
      _value:
        if r.machine_on == 1 then 1  // ON
        else if r.machine_on == 0 and r.Status_Code == 0 then 0  // OFF No Status
        else if r.machine_on == 0 and r.Status_Code == 1 then 2  // MC Trouble
        else if r.machine_on == 0 and r.Status_Code == 2 then 3  // Ganti LO/Warna
        else if r.machine_on == 0 and r.Status_Code == 3 then 4  // Bahan Baku Belum Ready
        else if r.machine_on == 0 and r.Status_Code == 4 then 5  // Tunggu Hasil Lab
        else if r.machine_on == 0 and r.Status_Code == 5 then 6  // Tunggu Order Prod
        else if r.machine_on == 0 and r.Status_Code == 6 then 7  // Tunggu Instruksi
        else -1
    }))
```

**Cara Kerja:**
1. Membaca status `machine_on` (1 = ON, 0 = OFF)
2. Membaca kode keterangan `ket_mesin_off` (alasan OFF)
3. Menggabungkan keduanya untuk menentukan status akhir

**Mapping Status:**
- `1` → **ON** (hijau)
- `0` → **OFF (No Status)** (merah)
- `2` → **OFF (MC Trouble)** (merah)
- `3` → **OFF (Ganti LO/Warna)** (merah)
- `4` → **OFF (Bahan Baku Belum Ready)** (merah)
- `5` → **OFF (Tunggu Hasil Lab)** (merah)
- `6` → **OFF (Tunggu Order Prod)** (merah)
- `7` → **OFF (Tunggu Instruksi)** (merah)

---

### 5️⃣ Panel PROCESS
**Fungsi:** Tabel yang menampilkan log proses yang sedang/pernah berjalan

**Query:**
```flux
from(bucket: "OtomasiEng")
  |> range(start: -1d)
  |> filter(fn: (r) => r["_measurement"] == "medium_frequency_data")
  |> filter(fn: (r) => r["machine_id"] == "1")
  |> filter(fn: (r) => r["_field"] == "process")
  |> keep(columns: ["_time", "_value"])
  |> rename(columns: {_value: "Process", _time: "Time"})
```

**Cara Kerja:**
1. Mengambil semua data field `process` dalam 1 hari terakhir
2. Menampilkan waktu dan kode proses dalam bentuk tabel

**Mapping Proses (contoh):**
- `260` → HEATING
- `265` → KEEPING
- `270` → COOLING
- `275` → TIME
- `290` → RINSING
- `295` → CALL
- `300` → SAMPLING
- `305` → FINISH
- Dan lain-lain (lihat mapping lengkap di panel)

---

### 6️⃣ Panel BATCH
**Fungsi:** Menampilkan nama batch yang sedang diproses

**Query:**
```flux
from(bucket: "OtomasiEng")
  |> range(start: -1d)
  |> filter(fn: (r) => r["_measurement"] == "cycle_context_data")
  |> filter(fn: (r) => r["machine_id"] == "1")
  |> filter(fn: (r) => r["_field"] == "batch")
  |> last()
  |> distinct(column: "_value")
```

**Cara Kerja:**
- Ambil nilai terakhir dari field `batch`
- Batch adalah string identifier untuk lot produksi

---

### 7️⃣ Panel pH
**Fungsi:** Menampilkan nilai pH terakhir dari sensor

**Query:**
```flux
from(bucket: "OtomasiEng")
  |> range(start:-1d)
  |> filter(fn: (r) => r["_measurement"] == "medium_frequency_data")
  |> filter(fn: (r) => r["machine_id"] == "1")
  |> filter(fn: (r) => r["_field"] == "ph")
  |> last()
```

**Cara Kerja:**
- Ambil nilai sensor pH terakhir
- Data pH disimpan di measurement `medium_frequency_data`

---

### 8️⃣ Tabel Riwayat Status OFF
**Fungsi:** Tabel yang menampilkan kapan mesin OFF, berapa lama, dan alasannya

**Query:**
```flux
verifiedWindowNs = 180000000000  // 3 menit dalam nanodetik

// A. Buat seri data ket_mesin_off dengan index
base = from(bucket: "OtomasiEng")
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(fn: (r) => r._measurement == "cycle_context_data")
    |> filter(fn: (r) => r._field == "ket_mesin_off")
    |> filter(fn: (r) => r.machine_id == "1")
    |> sort(columns: ["_time"])
    |> cumulativeSum(...)  // Buat index

// B. Verifikasi start yang valid (>= 3 menit ke sampel berikutnya)
verifiedStarts = ...
    |> filter(fn: (r) => r.dur_ns >= verifiedWindowNs)

// C. Cari rising edge machine_on (0 -> 1)
onEdges = from(bucket: "OtomasiEng")
    |> filter(fn: (r) => r._field == "machine_on")
    |> difference(nonNegative: false)
    |> filter(fn: (r) => r._value == 1)

// D. JOIN untuk mendapatkan waktu END (mesin ON kembali)
clipped = join(tables: {s: verifiedStarts, o: onEdges}, ...)

// E. Hitung durasi dan tampilkan
final = ...
    |> map(fn: (r) => ({
        START: r.RangeStart,
        END: r.RangeEnd,
        Status_Code: r.DATA_TERVERIFIKASI,
        duration_minute: float(END - START) / 60000000000.0
    }))
```

**Cara Kerja:**
1. **Deteksi START**: Ambil data `ket_mesin_off` yang bertahan >= 3 menit (untuk menghindari noise)
2. **Deteksi END**: Cari waktu mesin kembali ON (rising edge `machine_on`)
3. **Hitung Durasi**: Selisih waktu END - START
4. **Tampilkan**: Tabel dengan kolom START, DURATION (menit), dan Status Code

**Kolom Tabel:**
- `START MACHINE OFF`: Waktu mulai mesin OFF
- `DURATION (MINUTE)`: Lama mesin OFF dalam menit
- `STATUS OFF`: Keterangan alasan OFF (sama dengan mapping di Panel STATUS)

---

### 9️⃣ Grafik Time-Series
**Fungsi:** Grafik real-time untuk Temp1, Temp2, Level Air, dan Seam

**Query:**
```flux
// Helper function untuk mengambil 1 field dengan fill forward
getField = (fieldName) => {
    // Ambil 1 data sebelum timeRange sebagai baseline
    lastBefore = from(bucket: "OtomasiEng")
      |> range(start: -1d)
      |> filter(fn: (r) => r["_measurement"] == "high_frequency_data")
      |> filter(fn: (r) => r["machine_id"] == "1")
      |> filter(fn: (r) => r["_field"] == fieldName)
      |> last()

    // Ambil data dalam timeRange
    rangeData = from(bucket: "OtomasiEng")
      |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
      |> filter(...)

    // Gabungkan dan fill gap dengan nilai sebelumnya
    return union(tables: [lastBefore, rangeData])
      |> aggregateWindow(every: 5m, fn: last, createEmpty: true)
      |> fill(usePrevious: true)
}

// Ambil data untuk setiap field
temp1 = getField(fieldName: "temp1")
temp2 = getField(fieldName: "temp2")
seamR = getField(fieldName: "seam_right")
level = getField(fieldName: "level")  // dari medium_frequency_data

// Gabungkan semua
union(tables: [temp1, temp2, seamR, level])
```

**Cara Kerja:**
1. **Fill Forward Strategy**: Ambil 1 data sebelum time range sebagai baseline agar grafik tidak mulai dari 0
2. **Aggregate Window**: Data di-sampling setiap 5 menit untuk Temp & Seam
3. **Fill Gap**: Jika ada gap data, isi dengan nilai sebelumnya
4. **Level**: Disampel setiap 1 menit (frekuensi lebih rendah)

**Field yang Ditampilkan:**
- **TEMP 1** (°C): Suhu sensor 1 (warna merah)
- **TEMP 2** (°C): Suhu sensor 2 (warna ungu)
- **SEAM RIGHT**: Posisi seam kanan (%)
- **LEVEL**: Level air (0-4, TC)

---

## 💡 Catatan Penting

### Bucket InfluxDB
- **`OtomasiEng`**: Bucket utama untuk data mesin
- **`operatorDF`**: Bucket referensi untuk data operator

### Measurement
- **`high_frequency_data`**: Data sensor yang update cepat (temp, seam)
- **`medium_frequency_data`**: Data sensor/status yang update sedang (pH, level, process, machine_on)
- **`cycle_context_data`**: Data konteks proses (batch, shift, operator, jenis celup, status OFF)

### Time Range
- Sebagian besar query menggunakan `range(start: -1d)` untuk mengambil data 1 hari terakhir
- Dashboard di-refresh setiap **10 detik** (lihat `"refresh": "10s"`)

### Teknik Query
1. **Fill Forward**: Menghindari grafik kosong dengan mengambil 1 data sebelum range
2. **Aggregate Window**: Sampling data untuk performa lebih baik
3. **JOIN**: Menggabungkan data dari measurement berbeda
4. **Verification Window**: Filter data yang valid (>= 3 menit) untuk menghindari noise
5. **Difference + Filter**: Mendeteksi rising edge (0 → 1) untuk event ON

---

## 📚 Referensi
- **InfluxDB Flux Language**: https://docs.influxdata.com/flux/
- **Grafana Dashboard JSON**: https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/view-dashboard-json-model/
