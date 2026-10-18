"""Jurnal paper forward-test: catat sinyal live dan selesaikan outcome dari klines.

  python -m smcbot.paper status
  python -m smcbot.paper settle          # update baris OPEN dari harga terkini
  python -m smcbot.paper summary
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .backtest import DATA_DIR, metrics, fmt, simulate_trade
from .binance_rest import BinanceREST
from .config import SETTINGS, Settings
from .risk import TradePlan
from .scorer import Signal

JOURNAL = DATA_DIR / "paper_journal.csv"
COLS = [
    "time", "symbol", "score", "degraded", "entry", "sl", "tp", "net_rr", "risk_usd",
    "reasons", "status", "outcome", "exit_time", "r", "hold_candles",
]


def _ensure() -> pd.DataFrame:
    DATA_DIR.mkdir(exist_ok=True)
    if JOURNAL.exists():
        return pd.read_csv(JOURNAL, parse_dates=["time", "exit_time"])
    return pd.DataFrame(columns=COLS)


def _save(df: pd.DataFrame) -> None:
    df.to_csv(JOURNAL, index=False)


def append_signal(sig: Signal) -> None:
    """Tambah sinyal baru ke jurnal (status OPEN). Dedup: simbol+waktu sama dilewati."""
    df = _ensure()
    t = pd.Timestamp(sig.time)
    if len(df) and ((df["symbol"] == sig.symbol) & (pd.to_datetime(df["time"], utc=True) == t)).any():
        return
    p = sig.plan
    row = {
        "time": t.isoformat(), "symbol": sig.symbol, "score": sig.score, "degraded": sig.degraded,
        "entry": p.entry, "sl": p.sl, "tp": p.tp, "net_rr": p.net_rr, "risk_usd": p.risk_usd,
        "reasons": " | ".join(sig.reasons), "status": "OPEN", "outcome": "", "exit_time": pd.NaT,
        "r": float("nan"), "hold_candles": 0,
    }
    _save(pd.concat([df, pd.DataFrame([row])], ignore_index=True))


def settle(rest: BinanceREST | None = None, s: Settings = SETTINGS) -> int:
    """Selesaikan baris OPEN dengan simulasi pada klines sejak sinyal. Kembalikan jumlah yang ditutup."""
    df = _ensure()
    if df.empty:
        return 0
    open_mask = df["status"] == "OPEN"
    if not open_mask.any():
        return 0
    own = rest is None
    rest = rest or BinanceREST()
    closed = 0
    try:
        for i in df.index[open_mask]:
            row = df.loc[i]
            entry, sl, tp = float(row["entry"]), float(row["sl"]), float(row["tp"])
            risk = entry - sl
            fee_unit = entry * s.taker_fee_pct / 100 * 2
            risk_usd = float(row["risk_usd"]) if pd.notna(row["risk_usd"]) else s.risk_usd
            qty = risk_usd / (risk + fee_unit) if risk + fee_unit > 0 else 0.0
            plan = TradePlan(
                entry=entry, sl=sl, tp=tp, risk_pct=risk / entry * 100 if entry else 0.0,
                gross_rr=0.0, net_rr=float(row["net_rr"]), qty=qty, notional=qty * entry,
                leverage=(qty * entry) / s.account_usd if s.account_usd else 0.0, risk_usd=risk_usd,
            )
            l = rest.closed_klines(row["symbol"], s.ltf, limit=s.scan_window + s.max_hold_candles + 10)
            if l.empty:
                continue
            t0 = pd.Timestamp(row["time"])
            if t0.tzinfo is None:
                t0 = t0.tz_localize("UTC")
            idxs = l.index[l["time"] >= t0]
            if len(idxs) == 0:
                continue
            sig_i = int(idxs[0])
            outcome, j, r = simulate_trade(plan, l, sig_i, s)
            # TIMEOUT di ujung histori sebelum max hold: tunggu candle berikutnya
            if outcome == "TIMEOUT" and j >= len(l) - 1 and (j - sig_i) < s.max_hold_candles:
                continue
            df.at[i, "status"] = "CLOSED"
            df.at[i, "outcome"] = outcome
            df.at[i, "exit_time"] = l["time"].iloc[j]
            df.at[i, "r"] = r
            df.at[i, "hold_candles"] = j - sig_i
            closed += 1
    finally:
        if own:
            rest.close()
    _save(df)
    return closed


def summary() -> str:
    df = _ensure()
    if df.empty:
        return "jurnal kosong"
    lines = [f"total {len(df)} baris  OPEN={(df['status']=='OPEN').sum()}  CLOSED={(df['status']=='CLOSED').sum()}"]
    closed = df[df["status"] == "CLOSED"]
    if len(closed):
        rs = closed["r"].astype(float).to_numpy()
        lines.append("CLOSED: " + fmt(metrics(rs)))
        lines.append(f"  outcome: {closed['outcome'].value_counts().to_dict()}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Jurnal paper forward-test")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("settle")
    sub.add_parser("summary")
    a = ap.parse_args()
    if a.cmd == "settle":
        n = settle()
        print(f"ditutup: {n}")
        print(summary())
    elif a.cmd == "summary":
        print(summary())
    else:
        df = _ensure()
        print(f"file: {JOURNAL}")
        print(summary())
        if len(df):
            print(df.tail(10).to_string(index=False))


if __name__ == "__main__":
    main()
