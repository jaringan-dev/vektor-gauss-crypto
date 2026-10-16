"""Perekam data posisi 'bandar vs retail' ke SQLite, tiap 15 menit.

Tujuan: Binance hanya menyimpan ~30 hari riwayat OI / rasio long-short / taker, jadi tesis
'posisi top trader vs retail' tidak bisa dibacktest dari masa lalu. Perekam ini menimbun data sendiri.

SEMANTIK TIMESTAMP (diverifikasi 2026-10-16 terhadap klines BTCUSDT/SOLUSDT, cocok di semua baris):
- taker_*  : volume candle 15m yang DIBUKA pada `ts`  -> baru diketahui pada ts + 15 menit.
- oi_*     : potret pada batas `ts`                    -> diketahui sejak ts.
- ratio akun/posisi (global, top): diperlakukan seperti potret OI (diketahui sejak ts).
Analisis wajib memakai `available_ms()` agar tidak mengintip masa depan.

Dirancang tahan gangguan:
- Tiap siklus mengambil beberapa titik terakhir dan UPSERT per (symbol, ts) -> siklus yang terlewat
  (PC mati, jaringan putus) sembuh sendiri selama jeda < `limit` x 15 menit.
- Kegagalan satu panggilan tidak menggugurkan siklus; blokir IP (418) menghentikan siklus dengan rapi.
- Tabel `runs` mencatat tiap siklus (berhasil/gagal) supaya celah data bisa diaudit.

  python -m smcbot.recorder backfill --days 29   # sekali di awal: isi ~30 hari ke belakang
  python -m smcbot.recorder once                 # satu siklus
  python -m smcbot.recorder run                  # loop: tiap batas 15 menit
  python -m smcbot.recorder status               # ringkasan isi DB dan celah
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable

from .binance_rest import BinanceREST
from .config import SETTINGS

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "positioning.db"
PERIOD_MS = 15 * 60_000
PACE_S = 0.35          # + latensi ~0.15 dtk => ~600 panggilan/5 menit, di bawah batas 1000/5 menit per IP
PAGE = 500             # batas maksimum Binance per halaman untuk /futures/data/*

log = logging.getLogger("recorder")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snap (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,            -- timestamp titik dari Binance (ms, awal periode 15m)
    oi REAL, oi_value REAL,                                  -- openInterestHist
    g_long REAL, g_ratio REAL,                               -- rasio akun global (retail)
    ta_long REAL, ta_ratio REAL,                             -- top trader, rasio AKUN
    tp_long REAL, tp_ratio REAL,                             -- top trader, rasio POSISI
    taker_buy REAL, taker_sell REAL, taker_ratio REAL,       -- takerlongshortRatio
    PRIMARY KEY (symbol, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS mark (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,            -- waktu perekaman dibulatkan ke 15m
    mark_price REAL, index_price REAL, funding_rate REAL, next_funding_ms INTEGER,
    PRIMARY KEY (symbol, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS runs (
    ts INTEGER NOT NULL,            -- batas 15m siklus
    started_ms INTEGER, finished_ms INTEGER,
    n_symbols INTEGER, n_calls INTEGER, n_errors INTEGER,
    aborted TEXT,                   -- alasan henti dini (mis. 418), NULL bila normal
    kind TEXT                       -- cycle | backfill
);
"""

# nama -> (path, {kolom DB: kunci di respons Binance})
ENDPOINTS: dict[str, tuple[str, dict[str, str]]] = {
    "oi": ("/futures/data/openInterestHist", {"oi": "sumOpenInterest", "oi_value": "sumOpenInterestValue"}),
    "global": ("/futures/data/globalLongShortAccountRatio", {"g_long": "longAccount", "g_ratio": "longShortRatio"}),
    "top_acct": ("/futures/data/topLongShortAccountRatio", {"ta_long": "longAccount", "ta_ratio": "longShortRatio"}),
    "top_pos": ("/futures/data/topLongShortPositionRatio", {"tp_long": "longAccount", "tp_ratio": "longShortRatio"}),
    "taker": ("/futures/data/takerlongshortRatio",
              {"taker_buy": "buyVol", "taker_sell": "sellVol", "taker_ratio": "buySellRatio"}),
}


def floor15(ms: int) -> int:
    return ms - ms % PERIOD_MS


def available_ms(ts: int, column: str) -> int:
    """Kapan nilai `column` pada baris `ts` benar-benar sudah diketahui (anti-lookahead)."""
    return ts + PERIOD_MS if column.startswith("taker_") else ts


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def upsert_points(db: sqlite3.Connection, symbol: str, mapping: dict[str, str], points: list[dict]) -> int:
    """Upsert titik satu endpoint; hanya kolom milik endpoint itu yang disentuh."""
    cols = list(mapping)
    sql = (f"INSERT INTO snap (symbol, ts, {', '.join(cols)}) VALUES (?, ?, {', '.join('?' * len(cols))}) "
           f"ON CONFLICT(symbol, ts) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in cols))
    n = 0
    for p in points:
        try:
            db.execute(sql, (symbol, int(p["timestamp"]), *[float(p[mapping[c]]) for c in cols]))
            n += 1
        except (KeyError, TypeError, ValueError):
            continue  # titik rusak dilewati, bukan fatal
    return n


Getter = Callable[[str, dict], list | dict]


def record_symbol(db: sqlite3.Connection, get: Getter, symbol: str, params: dict) -> tuple[int, int]:
    """Ambil kelima endpoint untuk satu pair. Kembalikan (panggilan, error)."""
    calls = errors = 0
    for name, (path, mapping) in ENDPOINTS.items():
        calls += 1
        try:
            points = get(path, {"symbol": symbol, "period": "15m", **params})
            upsert_points(db, symbol, mapping, points)
        except RuntimeError as e:
            if "418" in str(e):
                raise  # blokir IP: hentikan, jangan menekan
            errors += 1
            log.warning("%s %s gagal: %s", symbol, name, e)
        except Exception as e:  # noqa: BLE001 - satu panggilan gagal tidak boleh menggugurkan siklus
            errors += 1
            log.warning("%s %s gagal: %s", symbol, name, e)
        time.sleep(PACE_S)
    return calls, errors


def record_marks(db: sqlite3.Connection, get: Getter, symbols: set[str], ts: int) -> None:
    for r in get("/fapi/v1/premiumIndex", {}):
        if r["symbol"] in symbols:
            db.execute(
                "INSERT OR REPLACE INTO mark VALUES (?,?,?,?,?,?)",
                (r["symbol"], ts, float(r["markPrice"]), float(r["indexPrice"]),
                 float(r["lastFundingRate"]), int(r["nextFundingTime"])),
            )


def run_cycle(db: sqlite3.Connection, get: Getter, symbols: list[str], limit: int = 8,
              kind: str = "cycle", now_ms: int | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    ts, started = floor15(now_ms), now_ms
    calls = errors = 0
    aborted = None
    try:
        record_marks(db, get, set(symbols), ts)
        for sym in symbols:
            c, e = record_symbol(db, get, sym, {"limit": limit})
            calls, errors = calls + c, errors + e
            db.commit()
    except RuntimeError as e:  # 418 atau sejenisnya
        aborted = str(e)[:200]
        log.error("siklus dihentikan: %s", aborted)
    db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
               (ts, started, int(time.time() * 1000), len(symbols), calls, errors, aborted, kind))
    db.commit()
    return {"ts": ts, "calls": calls, "errors": errors, "aborted": aborted}


def backfill(db: sqlite3.Connection, get: Getter, symbols: list[str], days: float) -> dict:
    """Isi riwayat ke belakang (Binance menyimpan maksimum ~30 hari)."""
    now = int(time.time() * 1000)
    start0 = floor15(now - int(days * 86_400_000))
    calls = errors = 0
    aborted = None
    try:
        for i, sym in enumerate(symbols, 1):
            cursor = start0
            while cursor < now:
                end = min(cursor + PAGE * PERIOD_MS, now)
                c, e = record_symbol(db, get, sym, {"limit": PAGE, "startTime": cursor, "endTime": end})
                calls, errors = calls + c, errors + e
                cursor = end + 1
            db.commit()
            log.info("backfill [%d/%d] %s selesai (panggilan=%d error=%d)", i, len(symbols), sym, calls, errors)
    except RuntimeError as e:
        aborted = str(e)[:200]
        log.error("backfill dihentikan: %s", aborted)
    db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
               (floor15(now), now, int(time.time() * 1000), len(symbols), calls, errors, aborted, "backfill"))
    db.commit()
    return {"calls": calls, "errors": errors, "aborted": aborted}


def status(db: sqlite3.Connection) -> str:
    lines = []
    n, syms, lo, hi = db.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(ts), MAX(ts) FROM snap").fetchone()
    if not n:
        return "DB kosong."
    fmt = lambda ms: time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))
    lines.append(f"snap: {n} baris, {syms} pair, {fmt(lo)} .. {fmt(hi)} UTC")
    complete = db.execute(
        "SELECT COUNT(*) FROM snap WHERE oi IS NOT NULL AND g_long IS NOT NULL AND ta_long IS NOT NULL "
        "AND tp_long IS NOT NULL AND taker_ratio IS NOT NULL").fetchone()[0]
    lines.append(f"baris lengkap (kelima sumber): {complete} ({complete / n:.1%})")
    lines.append(f"mark: {db.execute('SELECT COUNT(*) FROM mark').fetchone()[0]} baris")
    for kind, k, err, ab in db.execute(
            "SELECT kind, COUNT(*), SUM(n_errors), SUM(aborted IS NOT NULL) FROM runs GROUP BY kind"):
        lines.append(f"runs[{kind}]: {k} siklus, {err} error panggilan, {ab} dihentikan dini")
    last = db.execute("SELECT ts, n_errors, aborted FROM runs WHERE kind='cycle' ORDER BY ts DESC LIMIT 1").fetchone()
    if last:
        lines.append(f"siklus terakhir: {fmt(last[0])} UTC, error={last[1]}, aborted={last[2]}")
    return "\n".join(lines)


def _getter(rest: BinanceREST) -> Getter:
    return lambda path, params: rest._get(path, params)


def _universe(rest: BinanceREST) -> list[str]:
    # Tanpa lantai volume: untuk riset, data lebih luas lebih baik; filter likuiditas diterapkan saat analisis.
    return rest.top_symbols(SETTINGS.top_n_pairs, 0.0)


def run_forever(db: sqlite3.Connection, rest: BinanceREST) -> None:
    get = _getter(rest)
    while True:
        now = int(time.time() * 1000)
        wait = floor15(now) + PERIOD_MS + 20_000 - now  # 20 dtk setelah batas: titik periode baru sudah terbit
        log.info("tidur %.0f detik sampai siklus berikutnya", wait / 1000)
        time.sleep(max(wait / 1000, 0))
        try:
            res = run_cycle(db, get, _universe(rest))
            log.info("siklus selesai: %s", res)
        except Exception:  # noqa: BLE001 - loop tidak boleh mati karena satu siklus
            log.exception("siklus gagal total")


def main() -> None:
    ap = argparse.ArgumentParser(description="Perekam data posisi bandar-vs-retail")
    sub = ap.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill"); bf.add_argument("--days", type=float, default=29)
    sub.add_parser("once"); sub.add_parser("run"); sub.add_parser("status")
    a = ap.parse_args()
    DB_PATH.parent.mkdir(exist_ok=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # jangan catat tiap permintaan HTTP
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(DB_PATH.parent / "recorder.log", encoding="utf-8")])
    db, rest = connect(), BinanceREST()
    if a.cmd == "status":
        print(status(db))
    elif a.cmd == "once":
        print(run_cycle(db, _getter(rest), _universe(rest)))
    elif a.cmd == "backfill":
        print(backfill(db, _getter(rest), _universe(rest), a.days))
    else:
        run_forever(db, rest)


if __name__ == "__main__":
    main()
