# ZTE Router OpenAPI & Multi-Router Management API Server

[![FastAPI](https://img.shields.io/badge/FastAPI-0.142.2-009688.svg?logo=fastapi)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Read-Only](https://img.shields.io/badge/Safety-Read--Only%20Guaranteed-brightgreen.svg)]()

Server RESTful API dan Web UI konsol modern untuk manajemen dan pemantauan router fiber optik ZTE GPON (teruji pada **ZTE F609**, **ZTE F677V2**, dan **ZTE F8648P**).

Menyediakan ekstraksi kredensial WiFi (SSID & Password), metrik modul optik PON (Rx/Tx power, suhu, voltase), status antarmuka LAN & WAN, serta manajemen banyak router (multi-router) tanpa perlu restart server atau edit file lingkungan manual.

---

## Fitur Utama

- **Ekstraksi WiFi Lengkap**: Mengambil nama SSID, status aktif/nonaktif, tipe enkripsi/security, serta kata sandi (WPA/WPA2-PSK passphrase) yang terpasang pada router.
- **Ekstraksi Kredensial PPPoE**: Mengambil Username dan Password PPPoE akun WAN Internet dari konfigurasi router untuk kemudahan pencatatan dan diagnostik teknisi (dilengkapi tombol salin).
- **Konsol Web UI Non-Modal (`/ui`)**: Antarmuka bersih, cepat, responsif, dan non-modal (tanpa popup dialog yang mengganggu atau rusak). Dilengkapi tombol **Salin Password** sekali klik.
- **Multi-Router Registry**: Daftarkan banyak router sekaligus dengan penyimpanan persisten `data/routers.json` (izin file aman `0600`).
- **Mekanisme Re-Authentication Otomatis & Tombol Re-Auth**:
  - Deteksi otomatis saat sesi router kedaluwarsa atau dialihkan ke halaman login (`ZTESessionError`), secara transparan melakukan login ulang tanpa perlu edit/simpan profil router.
  - Endpoint eksplisit `POST /routers/{id}/reauth` dan tombol **🔑 Re-Auth** di Web UI untuk penyegaran sesi seketika.
- **Metrik Optik PON Akurat**: Pembacaan Rx Power (dBm) dengan indikator ambang sinyal (Normal, Waspada, Kritis), Tx Power, suhu modul, tegangan supply, dan status registrasi ONU (O1-O5).
- **Status Port LAN & Antarmuka WAN**: Link status (Up/Down), link speed, duplex mode, alamat IP WAN, Gateway, DNS, serta traffic byte counter.
- **Dokumentasi OpenAPI 3.0 / Swagger Bersih (`/docs`)**: Schema query parameter telah dibersihkan dari `anyOf` schema null issues agar 100% patuh pada spesifikasi OpenAPI.
- **Jaminan Read-Only Mutlak**: Tidak ada operasi penulisan atau pengubahan konfigurasi jaringan/WiFi ke perangkat router.

---

## Arsitektur & Dukungan Perangkat

Sistem secara cerdas mendeteksi varian model router ZTE:

| Model Router | Metode Otentikasi | Halaman Status GCH / Lua | Ekstraksi WiFi |
| :--- | :--- | :--- | :--- |
| **ZTE F609** | Form Login (MD5 + Salt) | `pon_status_lan_info_t.gch`, `IPv46_status_wan2_if_t.gch` | `status_wlanm_info1_t.gch` + `net_wlanm_secrity1_t.gch` |
| **ZTE F677V2** | Form Login (SHA256 + Salt) | `status_lan_info_t.gch`, `status_ethwan_if_t.gch` | `pon_net_wlan_conf1_t.gch` (Multi-SSID & KeyPassphrase) |
| **ZTE F8648P** | XML/Lua Token (SHA256) | `devmgr_statusmgr_lua.lua`, `status_lan_info_lua.lua` | API Lua session endpoints |

---

## Definisi Kredensial (Otomatis & Manual)

Sistem mendukung 2 mode autentikasi kredensial baik saat mendaftarkan single router maupun saat pemindaian massal (subnet / CIDR range):

| Mode Kredensial | Target Model | Username | Password | Metode Autentikasi |
| :--- | :--- | :--- | :--- | :--- |
| **🤖 Otomatis** | **ZTE F609** | `admin` | `admin` | MD5 + dynamic token/salt |
| **🤖 Otomatis** | **ZTE F677V2** | `superadmin` | `suportadmin` | SHA256 + dynamic token/salt |
| **🤖 Otomatis** | **🔍 Deteksi Otomatis** | *Otomatis (sesuai model terdeteksi)* | *Otomatis* | Probe IP cepat untuk menentukan F609 vs F677V2 |
| **✏️ Manual** | Bebas / Kustom | Sesuai input pengguna | Sesuai input pengguna | Mencoba MD5 (F609) lalu SHA256 (F677V2) |

---

## Memulai Cepat

### 1. Menjalankan Langsung (Python Virtualenv)

```bash
# Clone repositori
git clone https://github.com/kelvinzer0/zte-router-api.git
cd zte-router-api

# Buat virtual environment & install dependensi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Jalankan server API
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Buka peramban di:
- **Web Console UI**: [http://127.0.0.1:8000/ui](http://127.0.0.1:8000/ui) (atau [http://127.0.0.1:8000/](http://127.0.0.1:8000/))
- **Dokumentasi Swagger API**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **Spesifikasi OpenAPI JSON**: [http://127.0.0.1:8000/openapi.json](http://127.0.0.1:8000/openapi.json)

### 2. Menggunakan Docker

```bash
docker build -t zte-router-api .

docker run -d \
  --name zte-router-api \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  zte-router-api
```

---

## Mekanisme Re-Authentication & Manajemen Sesi

Sebelumnya, jika sesi router kedaluwarsa, pengguna mungkin merasa perlu melakukan "edit dan simpan" profil router agar client pool di-refresh. Masalah ini telah diselesaikan secara menyeluruh melalui 2 mekanisme:

1. **Auto-Recovery on Session Expiry**:
   Saat router ZTE memutus sesi (idle timeout), router mengalihkan request data ke halaman login (`Frm_Logintoken`). Client mendeteksi pengalihan ini sebagai `ZTESessionError`, otomatis menghapus token kedaluwarsa, mengeksekusi login ulang, dan mengulangi request data secara transparan tanpa intervensi pengguna.
2. **Explicit Re-Auth (`POST /routers/{id}/reauth`)**:
   Tersedia tombol **🔑 Re-Auth** di Web UI dan endpoint API untuk memaksa penghapusan cookie sesi lama dan melakukan otentikasi ulang baru ke router dalam hitungan milidetik.

---

## Daftar Endpoint API

### Manajemen Multi-Router (`/routers`)

| Method | Endpoint | Deskripsi |
| :--- | :--- | :--- |
| `GET` | `/routers` | Daftar seluruh router yang terdaftar (tanpa mengekspos password). |
| `POST` | `/routers` | Menambahkan router baru ke registri. |
| `POST` | `/routers/scan` | **Scan rentang IP / CIDR** (mis. `10.200.204.0/24`) khusus perangkat **ZTE F609** dan **F677V2** dan daftarkan massal secara otomatis. |
| `POST` | `/routers/scan-stream` | **Scan rentang IP dengan live SSE stream** untuk monitoring progres dan penemuan perangkat real-time. |
| `POST` | `/routers/search-ssid` | **Cari SSID di seluruh router**: Temukan di IP mana suatu SSID berada dengan opsi re-auth per router. |
| `POST` | `/routers/search-ssid-stream` | **Cari SSID dengan live SSE stream**: Pemantauan real-time router-demi-router, IP locator, ekstraksi password WiFi & indikator re-auth. |
| `GET` | `/routers/backup-csv` | **Backup CSV WiFi**: Ekspor berkas CSV berisi khusus kolom `ip,ssid,password` untuk seluruh router atau router tertentu. |
| `GET` | `/routers/{router_id}` | Mengambil detail konfigurasi satu router. |
| `PUT` | `/routers/{router_id}` | Memperbarui router (kosongkan password untuk mempertahankan yang ada). |
| `DELETE` | `/routers/{router_id}` | Menghapus router dari registri. |
| `POST` | `/routers/{router_id}/test` | Uji konektivitas & otentikasi cepat dengan metrik latensi ms. |
| `POST` | `/routers/{router_id}/reauth` | **Paksa login ulang** dan bersihkan sesi kedaluwarsa satu router. |
| `POST` | `/routers/reauth-all` | **Re-Auth Massal**: Login ulang ke seluruh router sekaligus & perbarui label SSID. |
| `POST` | `/routers/reauth-all-stream` | **Re-Auth Massal (Live SSE)**: Live streaming progres pembaruan sesi dan label SSID router. |
| `GET` | `/routers/{router_id}/overview` | Ringkasan terpadu (Device, PON, WiFi, LAN, WAN) dalam satu panggilan. |
| `GET` | `/routers/{router_id}/backup-config` | **Unduh berkas cadangan `config.bin`** langsung dari router. |
| `POST` | `/routers/{router_id}/decrypt-config` | **Unduh & dekripsi otomatis `config.bin`** untuk mengekstrak password PPPoE plaintext, WiFi, dan admin. |

### Ekstraksi & Dekripsi Konfigurasi (`config.bin`)

| Method | Endpoint | Deskripsi |
| :--- | :--- | :--- |
| `GET` | `/routers/{router_id}/backup-config` | Unduh file cadangan binary `config.bin` router. |
| `POST` | `/routers/{router_id}/decrypt-config` | Unduh & dekripsi otomatis konfigurasi router target. |
| `POST` | `/decrypt-config` | Upload dan dekripsi file `config.bin` mandiri secara bebas. |

### Operasi Telnet & sendcmd

| Method | Endpoint | Deskripsi |
| :--- | :--- | :--- |
| `GET` | `/routers/{router_id}/telnet/status` | Cek status port Telnet 23 (Terbuka/Tertutup/Latency). |
| `POST` | `/routers/{router_id}/telnet/enable-factory` | Aktifkan layanan Telnet via protokol ZTE Factory Mode (`/webFac`). |
| `POST` | `/routers/{router_id}/telnet/extract` | Ekstrak plaintext PPPoE, Web Admin, dan WiFi via `sendcmd`. |
| `POST` | `/routers/{router_id}/telnet/command` | Eksekusi perintah shell / sendcmd kustom pada router. |

### Data Status & Ekstraksi Jaringan

Setiap endpoint di bawah menerima query parameter opsional `router_id` (misalnya `/wifi?router_id=5157737460f2`). Jika tidak disertakan, sistem menggunakan router default pertama.

| Method | Endpoint | Deskripsi |
| :--- | :--- | :--- |
| `GET` | `/wifi` | Mengambil daftar SSID, password WiFi, status aktif, dan keamanan. |
| `GET` | `/pon` | Parameter modul optik PON (Rx/Tx power dBm, suhu, tegangan, arus bias). |
| `GET` | `/device` | Model perangkat, nomor seri (SN), versi hardware & firmware. |
| `GET` | `/lan` | Status port LAN fisik (Link Up/Down, kecepatan Mbps, duplex, byte counter). |
| `GET` | `/wan` | Informasi koneksi WAN (PPPoE/DHCP, IP Address, Gateway, DNS, MAC) beserta kredensial PPPoE (`username`, `password`). |
| `GET` | `/health` | Health check API server & status keterhubungan router. |
| `GET` | `/status` | Seluruh data diagnostik mentah router. |
| `POST` | `/reauth` | Otentikasi ulang instan untuk router target. |
| `POST` | `/login` | Otentikasi manual router. |
| `POST` | `/logout` | Mengakhiri sesi router. |

---

## Contoh Penggunaan (cURL & Python)

### 1. Ekstraksi WiFi (SSID & Password)

```bash
curl -s "http://127.0.0.1:8000/wifi?router_id=5157737460f2"
```

**Contoh Output:**
```json
[
  {
    "ssid": "ZTE_2.4G_UTRnDz",
    "password": "Wl098765_",
    "enabled": true,
    "band": "2.4G",
    "security": "WPA/WPA2-PSK"
  }
]
```

### 2. Ekstraksi Koneksi WAN & Akun PPPoE

```bash
curl -s "http://127.0.0.1:8000/wan?router_id=52d8b02611c4"
```

**Contoh Output:**
```json
[
  {
    "name": "2_INTERNET_R_VID_1",
    "connection_type": "PPPoE",
    "status": "Connected",
    "ip_address": "10.200.204.1",
    "gateway": "10.200.192.1",
    "dns_servers": "10.200.192.1",
    "mac_address": "f4:b5:aa:54:ca:eb",
    "username": "nurWahidUD@indo.net",
    "password": "******"
  }
]
```

### 3. Membaca Metrik Optik PON

```bash
curl -s "http://127.0.0.1:8000/pon?router_id=52d8b02611c4"
```

**Contoh Output:**
```json
{
  "onu_state": "Operation State (O5)",
  "rx_power_dbm": -18.32,
  "tx_power_dbm": 2.29,
  "voltage_mv": 3274,
  "current_ma": 15.91,
  "temperature_c": 53.0
}
```

### 3. Menguji Koneksi & Re-Auth

```bash
# Uji koneksi
curl -X POST "http://127.0.0.1:8000/routers/52d8b02611c4/test"

# Paksa re-auth
curl -X POST "http://127.0.0.1:8000/routers/52d8b02611c4/reauth"
```

---

## Konfigurasi File Lingkungan (Fallback Default)

Bila direktori `data/routers.json` belum diisi, server dapat membaca konfigurasi router bawaan dari environment:

| Variabel Lingkungan | Nilai Bawaan | Keterangan |
| :--- | :--- | :--- |
| `ZTE_HOST` | `192.168.178.1` | Alamat IP atau hostname router |
| `ZTE_USERNAME` | `admin` | Username web admin router |
| `ZTE_PASSWORD` | `admin` | Password web admin router |
| `ZTE_TIMEOUT` | `10` | Timeout koneksi dalam detik |
| `ZTE_ROUTERS_FILE` | `data/routers.json` | Lokasi berkas penyimpanan database router |

---

## Lisensi

Proyek ini dilisensikan di bawah [MIT License](LICENSE).
