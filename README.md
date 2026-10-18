# vektor-gauss-crypto

Toolkit riset + scanner live sinyal **long** berbasis Smart Money Concepts (SMC) untuk Binance USDT-M Futures: deteksi sweep + reclaim, skor confluence (harga/CVD + posisi), backtest, kalibrasi, perekam posisi, alert Telegram, dan jurnal paper.

## Prasyarat

- Python **3.12+**
- Akses jaringan ke `fapi.binance.com` (REST publik, tanpa API key)
- Opsional: endpoint MCP + token untuk lapisan posisi “bandar vs retail”
- Opsional: bot Telegram untuk alert

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
| `TELEGRAM_BOT_TOKEN` | Untuk alert | Dipakai `smcbot.live` |
| `TELEGRAM_CHAT_ID` | Untuk alert | Dipakai `smcbot.live` |

Folder `data/` dibuat otomatis (cache klines `.pkl`, CSV hasil, SQLite recorder, jurnal paper). Folder ini di-ignore git.

## Konfigurasi

Semua parameter tuning ada di `smcbot/config.py` (`SETTINGS`):

| Kunci | Default | Arti |
|---|---|---|
| `score_threshold_degraded` | **37** | Ambang beku kalibrasi (harga-only / tanpa posisi) |
| `score_threshold` | **50** | Ambang bila data posisi tersedia (~13 poin lapisan posisi) |
| `prefilter_score` | 35 | Skor harga minimum sebelum MCP/DB dipanggil |
| `daily_loss_limit_r` | 3.0 | Henti sinyal baru hari itu setelah rugi kumulatif (backtest + live cap harian) |
| `max_signals_per_day` | 5 | Cap frekuensi |

Skor maksimum 100: **65** harga+flow + **35** posisi (MCP atau `positioning.db`).

## Cara menjalankan

Jalankan dari root repo, venv aktif.

### Tes unit

```bash
pytest -q
```

### Scanner live + Telegram + jurnal paper

```bash
python -m smcbot.live once                 # satu putaran top pair
python -m smcbot.live run                  # loop tiap close 15m
python -m smcbot.live once --no-mcp        # harga saja (degraded)
python -m smcbot.live once --no-telegram
python -m smcbot.live once --no-paper
```

Live memakai MCP bila tersedia, fallback ke `data/positioning.db`. Sinyal masuk `data/paper_journal.csv` dan dikirim ke Telegram bila `.env` terisi.

Windows Task Scheduler: `scripts/run_live.cmd` (sesuaikan path Python).

### Jurnal paper forward-test

```bash
python -m smcbot.paper status
python -m smcbot.paper settle     # tutup baris OPEN dari klines terkini
python -m smcbot.paper summary
```

Sebelum paper dengan uang perhatian: konfirmasi vonis hold-out (`python -m smcbot.calibrate holdout`). Vonis LULUS/MENJANJIKAN → paper saja; gagal → revisi hipotesis, bukan utak-atik ambang di hold-out.

### Backtest (harga + CVD, opsional posisi)

```bash
python -m smcbot.backtest
python -m smcbot.backtest --days 180 --pairs 20 --workers 4
python -m smcbot.backtest --with-positioning   # join data/positioning.db (anti-lookahead)
python -m smcbot.backtest --refresh
python -m smcbot.backtest --no-prefilter
```

Trade terpilih → `data/backtest_trades.csv`. Aturan portofolio: satu posisi/pair, cap harian, **batas rugi harian** (`daily_loss_limit_r`).

**Catatan bias:** pair dari ranking volume *sekarang* (survivorship).

### Kalibrasi skor

```bash
python -m smcbot.calibrate scan --days 180 --pairs 20 --workers 4
python -m smcbot.calibrate analyze
python -m smcbot.calibrate scan --days 180 --pairs 20 --offset-days 180
python -m smcbot.calibrate holdout
python -m smcbot.calibrate scan-one BTCUSDT --days 180
```

`FROZEN_THRESHOLD` = `SETTINGS.score_threshold_degraded` (37). Jangan disetel ulang terhadap data hold-out.

### Perekam posisi (SQLite)

```bash
python -m smcbot.recorder backfill --days 29
python -m smcbot.recorder once
python -m smcbot.recorder run
python -m smcbot.recorder status
```

Windows: `scripts/run_recorder.cmd`. DB → `data/positioning.db` (dipakai live fallback + `--with-positioning`).

## Struktur modul

```
smcbot/
  config.py           # SETTINGS + env
  binance_rest.py     # REST publik futures
  mcp_client.py       # JSON-RPC MCP (posisi)
  smc.py / flow.py    # detektor + CVD
  positioning.py      # fetch MCP → Positioning
  positioning_db.py   # baca SQLite + available_ms
  risk.py / scorer.py # rencana + evaluate()
  backtest.py         # CLI backtest
  calibrate.py        # CLI kalibrasi / hold-out
  recorder.py         # CLI perekam posisi
  live.py             # CLI scanner live
  telegram_notify.py  # alert Telegram
  paper.py            # jurnal paper forward-test
scripts/
  run_recorder.cmd
  run_live.cmd
tests/
```

## Dependensi

Lihat `requirements.txt`: `httpx`, `pandas`, `python-dotenv`, `pytest`. NumPy ikut lewat pandas.
