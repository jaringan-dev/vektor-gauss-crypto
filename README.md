# vektor-gauss-crypto

Toolkit riset sinyal **long** berbasis Smart Money Concepts (SMC) untuk Binance USDT-M Futures: deteksi sweep + reclaim, skor confluence (harga/CVD + opsional posisi), backtest, kalibrasi ambang skor, dan perekam data posisi (OI / long-short).

> Belum ada CLI scanner live / alert Telegram. Token Telegram di `.env` sudah disiapkan untuk tahap berikutnya.

## Prasyarat

- Python **3.12+**
- Akses jaringan ke `fapi.binance.com` (REST publik, tanpa API key)
- Opsional: endpoint MCP + token untuk lapisan posisi “bandar vs retail”

## Setup

```bash
git clone https://github.com/jaringan-dev/vektor-gauss-crypto.git
cd vektor-gauss-crypto

python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # Windows; di Unix: cp .env.example .env
```

Isi `.env` bila perlu:

| Variabel | Wajib? | Keterangan |
|---|---|---|
| `MCP_URL` | Tidak | Default `https://binance.jaringan.dev/mcp` |
| `MCP_AUTH_TOKEN` | Untuk MCP | Bearer token klien MCP |
| `TELEGRAM_BOT_TOKEN` | Tidak (belum dipakai CLI) | Cadangan alert |
| `TELEGRAM_CHAT_ID` | Tidak (belum dipakai CLI) | Cadangan alert |

Folder `data/` dibuat otomatis (cache klines `.pkl`, CSV hasil, SQLite recorder). Folder ini di-ignore git.

## Konfigurasi

Semua parameter tuning ada di `smcbot/config.py` (`SETTINGS`): universe pair, risiko akun, ambang skor, fee/slippage backtest, timeframe LTF/HTF (`15m` / `1h`+`4h`).

Skor maksimum 100:

- **65** dari harga + flow (sweep, zona FVG/OB, CVD) — cukup untuk backtest/kalibrasi tanpa MCP
- **+35** dari posisi (funding, retail, top trader, OI) lewat MCP — bila MCP gagal, mode *degraded* memakai ambang lebih rendah

## Cara menjalankan

Jalankan semua perintah dari root repo, dengan venv aktif.

### Tes unit

```bash
pytest -q
```

### Backtest (lapisan harga + CVD)

Memakai top pair by volume saat ini, cache klines ke `data/*.pkl`, menulis trade terpilih ke `data/backtest_trades.csv`.

```bash
python -m smcbot.backtest
python -m smcbot.backtest --days 180 --pairs 20 --workers 4
python -m smcbot.backtest --refresh          # unduh ulang klines
python -m smcbot.backtest --no-prefilter     # evaluasi semua candle (lambat)
```

| Flag | Default | Arti |
|---|---|---|
| `--days` | `180` | Periode uji (hari) |
| `--pairs` | `20` | Jumlah pair top volume |
| `--workers` | `4` | Proses paralel |
| `--refresh` | off | Abaikan cache `.pkl` |
| `--no-prefilter` | off | Tanpa mask kandidat sweep |

**Catatan bias:** pair diambil dari ranking volume *sekarang* (survivorship); lapisan posisi MCP tidak diuji; daily loss limit belum dimodelkan.

### Kalibrasi skor

Alur: scan kandidat → analisis split waktu → (opsional) hold-out sekali.

```bash
# 1) Scan (lambat) → data/candidates.csv
python -m smcbot.calibrate scan --days 180 --pairs 20 --workers 4

# 2) Analisis cepat (bisa diulang)
python -m smcbot.calibrate analyze

# 3) Hold-out: periode SEBELUM jendela kalibrasi
python -m smcbot.calibrate scan --days 180 --pairs 20 --offset-days 180
python -m smcbot.calibrate holdout
```

Satu simbol saja:

```bash
python -m smcbot.calibrate scan-one BTCUSDT --days 180
```

Ambang beku hold-out ada di `FROZEN_THRESHOLD` dalam `smcbot/calibrate.py` — jangan disetel ulang terhadap data hold-out.

### Perekam posisi (SQLite)

Binance hanya menyimpan ~30 hari riwayat OI / rasio L-S / taker. Recorder menimbun snapshot 15m ke `data/positioning.db`.

```bash
# Sekali di awal: isi ~29 hari ke belakang
python -m smcbot.recorder backfill --days 29

# Satu siklus
python -m smcbot.recorder once

# Loop tiap batas 15 menit (untuk Task Scheduler / service)
python -m smcbot.recorder run

# Ringkasan isi DB + celah
python -m smcbot.recorder status
```

Di Windows, `scripts/run_recorder.cmd` memanggil `python -m smcbot.recorder run` dan menulis stdout ke `data/recorder_stdout.log`. Sesuaikan path Python di file itu bila beda mesin.

Siklus yang terlewat (PC mati/tidur) diisi ulang lewat UPSERT selama jeda masih dalam jangkauan backfill/limit API.

## Struktur modul

```
smcbot/
  config.py        # SETTINGS + env
  binance_rest.py  # REST publik futures
  mcp_client.py    # JSON-RPC MCP (posisi)
  smc.py           # ATR, swing, sweep, FVG/OB, bias HTF
  flow.py          # delta / CVD / absorption
  positioning.py   # fetch skor posisi lewat MCP
  risk.py          # rencana long (entry, SL, TP, size)
  scorer.py        # gerbang + skor + evaluate()
  backtest.py      # CLI backtest
  calibrate.py     # CLI kalibrasi / hold-out
  recorder.py      # CLI perekam posisi
scripts/
  run_recorder.cmd # peluncur Windows untuk recorder loop
tests/             # pytest
```

## Dependensi

Lihat `requirements.txt`: `httpx`, `pandas`, `python-dotenv`, `pytest`. NumPy ikut lewat pandas.
