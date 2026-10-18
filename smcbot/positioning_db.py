"""Baca snapshot posisi dari SQLite recorder dengan anti-lookahead (`available_ms`)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .positioning import Positioning
from .recorder import DB_PATH, available_ms, connect

# Berapa bar OI yang dipakai untuk changePct (harus sudah available pada as_of).
_OI_LOOKBACK = 8


def positioning_at(db: sqlite3.Connection, symbol: str, as_of_ms: int) -> Positioning | None:
    """Posisi yang sudah diketahui pada `as_of_ms` (biasanya close_time candle sinyal).

    - Rasio akun/posisi & OI: diketahui sejak `ts` baris.
    - Taker tidak dipakai di `Positioning` (skor memakai funding/retail/top/OI).
    - Funding diambil dari tabel `mark` dengan `ts <= as_of_ms`.
    """
    # Snapshot terbaru dengan ts tersedia (non-taker fields known at ts).
    row = db.execute(
        "SELECT ts, oi, g_long, tp_long FROM snap "
        "WHERE symbol=? AND ts<=? AND oi IS NOT NULL AND g_long IS NOT NULL AND tp_long IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1",
        (symbol, as_of_ms),
    ).fetchone()
    if row is None:
        return None
    ts, oi, g_long, tp_long = row
    # Pastikan kolom yang dipakai sudah available (non-taker => available_ms == ts).
    if available_ms(int(ts), "oi") > as_of_ms or available_ms(int(ts), "g_long") > as_of_ms:
        return None

    mark = db.execute(
        "SELECT funding_rate FROM mark WHERE symbol=? AND ts<=? AND funding_rate IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1",
        (symbol, as_of_ms),
    ).fetchone()
    if mark is None:
        return None
    funding = float(mark[0])

    oi_rows = db.execute(
        "SELECT ts, oi FROM snap WHERE symbol=? AND ts<=? AND oi IS NOT NULL ORDER BY ts DESC LIMIT ?",
        (symbol, as_of_ms, _OI_LOOKBACK),
    ).fetchall()
    if len(oi_rows) < 2:
        return None
    # Hanya bar yang sudah available; filter redundan untuk non-taker tapi menjaga kontrak.
    oi_vals = [float(r[1]) for r in oi_rows if available_ms(int(r[0]), "oi") <= as_of_ms]
    if len(oi_vals) < 2 or oi_vals[-1] == 0:
        return None
    oi_change_pct = (oi_vals[0] / oi_vals[-1] - 1.0) * 100.0

    return Positioning(
        funding_rate=funding,
        retail_long_pct=float(g_long) * 100.0,
        top_long_pct=float(tp_long) * 100.0,
        oi_change_pct=oi_change_pct,
    )


def make_db_fetcher(db_path: Path | str = DB_PATH):
    """Callback `symbol -> Positioning | None` untuk `evaluate(..., fetch_positioning=...)`.

    Membuka koneksi sendiri (aman untuk ProcessPool). Caller harus menutup via `.close()`.
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"positioning DB tidak ada: {path}")
    db = connect(path)

    def fetch(symbol: str, as_of_ms: int | None = None) -> Positioning | None:
        if as_of_ms is None:
            as_of_ms = int(time.time() * 1000)
        return positioning_at(db, symbol, as_of_ms)

    fetch.close = db.close  # type: ignore[attr-defined]
    fetch.db = db  # type: ignore[attr-defined]
    return fetch


def make_asof_fetcher(db: sqlite3.Connection, as_of_ms: int):
    """Fetcher terikat ke satu timestamp (dipakai di backtest per candle)."""
    def fetch(symbol: str) -> Positioning | None:
        return positioning_at(db, symbol, as_of_ms)
    return fetch
