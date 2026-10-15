"""Backtester lapisan harga + CVD (tanpa data posisi MCP: riwayat OI/L-S hanya ~30 hari).

Prinsip anti-bias:
- HTF disejajarkan per waktu: hanya candle 1H/4H yang SUDAH close sebelum sinyal.
- Entry di close candle sinyal + slippage; SL dan TP satu candle -> dianggap SL dulu.
- Fee taker dua sisi dan slippage dimasukkan ke R.
- Baseline entry acak dengan geometri risiko serupa, untuk melihat apakah ada edge sama sekali.
- Pair dipilih dari top volume SEKARANG -> ada survivorship bias (dicatat di laporan).
"""
from __future__ import annotations

import argparse
import re
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .binance_rest import BinanceREST
from .config import SETTINGS, Settings
from .risk import TradePlan, plan_long
from .scorer import Signal, evaluate
from .smc import atr

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TF_MS = {"15m": 15 * 60_000, "1h": 60 * 60_000, "4h": 4 * 60 * 60_000}
HTF_TAIL = 200


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def load_history(rest: BinanceREST, symbol: str, tf: str, days: int, refresh: bool = False) -> pd.DataFrame:
    """Klines tertutup `days` hari terakhir, di-cache ke data/*.pkl."""
    DATA_DIR.mkdir(exist_ok=True)
    f = DATA_DIR / f"{symbol}_{tf}_{days}d.pkl"
    if f.exists() and not refresh:
        return pd.read_pickle(f)
    end = int(time.time() * 1000)
    df = rest.klines_range(symbol, tf, end - days * 86_400_000, end)
    df = df[df["close_time"] < end].reset_index(drop=True)
    df.to_pickle(f)
    return df


# --------------------------------------------------------------------------
# Pemindaian sinyal
# --------------------------------------------------------------------------
def candidate_mask(l: pd.DataFrame, reclaim_window: int = 2) -> np.ndarray:
    """Syarat perlu (murah) agar sweep bisa ada di candle i: salah satu dari 3 candle
    terakhir membuat low di bawah semua low 7 candle sebelumnya. Swing low yang disapu
    harus lebih rendah dari lows di sekitarnya, jadi ini tidak membuang sinyal sah."""
    low = l["low"]
    cond = low < low.shift(1).rolling(7).min()
    m = cond.copy()
    for j in range(1, reclaim_window + 1):
        m |= cond.shift(j, fill_value=False)
    return m.to_numpy()


def _htf_window(h: pd.DataFrame, h_close: np.ndarray, t: int) -> pd.DataFrame:
    pos = int(np.searchsorted(h_close, t, side="left"))  # hanya candle yang close < t
    return h.iloc[max(0, pos - HTF_TAIL) : pos].reset_index(drop=True)


def scan_symbol(args) -> tuple[str, list[Signal], Counter]:
    symbol, l, h1, h4, start_i, s, use_prefilter = args
    mask = candidate_mask(l) if use_prefilter else np.ones(len(l), bool)
    h1c, h4c = h1["close_time"].to_numpy(), h4["close_time"].to_numpy()
    sigs, why = [], Counter()
    for i in range(max(start_i, s.scan_window), len(l)):
        if not mask[i]:
            continue
        w = l.iloc[i - s.scan_window + 1 : i + 1].reset_index(drop=True)
        t = int(w["close_time"].iloc[-1])
        sig, reason = evaluate(symbol, w, _htf_window(h1, h1c, t), _htf_window(h4, h4c, t), None, s)
        if sig:
            sigs.append(_with_index(sig, i))
            why["SINYAL"] += 1
        else:
            why[re.sub(r"[\d.\-]+", "#", reason)] += 1
    return symbol, sigs, why


@dataclass(frozen=True)
class IndexedSignal:
    signal: Signal
    idx: int  # posisi candle sinyal pada klines 15m penuh


def _with_index(sig: Signal, idx: int) -> IndexedSignal:
    return IndexedSignal(sig, idx)


# --------------------------------------------------------------------------
# Simulasi trade
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Trade:
    symbol: str
    time: pd.Timestamp
    exit_time: pd.Timestamp
    score: float
    outcome: str      # TP | SL | TIMEOUT
    r: float          # hasil bersih dalam kelipatan risiko ($5)
    hold_candles: int


def simulate_trade(plan: TradePlan, l: pd.DataFrame, i: int, s: Settings = SETTINGS) -> tuple[str, int, float]:
    """Jalankan rencana dari candle i+1. Kembalikan (outcome, exit_idx, R bersih)."""
    slip = s.slippage_pct / 100
    entry = plan.entry * (1 + slip)
    low, high, close = l["low"].to_numpy(), l["high"].to_numpy(), l["close"].to_numpy()
    last = min(i + s.max_hold_candles, len(l) - 1)

    outcome, exit_idx, exit_px = "TIMEOUT", last, close[last] * (1 - slip)
    for j in range(i + 1, last + 1):
        if low[j] <= plan.sl:                       # SL dicek dulu: asumsi terburuk bila satu candle
            outcome, exit_idx, exit_px = "SL", j, plan.sl * (1 - slip)
            break
        if high[j] >= plan.tp:
            outcome, exit_idx, exit_px = "TP", j, plan.tp
            break
    fee = s.taker_fee_pct / 100 * plan.qty * (entry + exit_px)
    pnl = plan.qty * (exit_px - entry) - fee
    return outcome, exit_idx, pnl / s.risk_usd


def to_trade(sym: str, plan: TradePlan, score: float, l: pd.DataFrame, i: int, s: Settings) -> Trade:
    outcome, j, r = simulate_trade(plan, l, i, s)
    return Trade(sym, l["time"].iloc[i], l["time"].iloc[j], score, outcome, r, j - i)


def apply_portfolio_rules(items: list[tuple[Trade, int]], s: Settings) -> list[Trade]:
    """Urut kronologis (skor tertinggi dulu bila bersamaan); satu posisi per pair;
    maksimum `max_signals_per_day` per hari UTC. Batas rugi harian belum dimodelkan."""
    items = sorted(items, key=lambda x: (x[0].time, -x[0].score))
    busy: dict[str, pd.Timestamp] = {}
    per_day: Counter = Counter()
    taken: list[Trade] = []
    for tr, _ in items:
        if busy.get(tr.symbol, pd.Timestamp.min.tz_localize("UTC")) > tr.time:
            continue
        day = tr.time.date()
        if per_day[day] >= s.max_signals_per_day:
            continue
        per_day[day] += 1
        busy[tr.symbol] = tr.exit_time
        taken.append(tr)
    return taken


# --------------------------------------------------------------------------
# Metrik
# --------------------------------------------------------------------------
def metrics(rs: np.ndarray, seed: int = 7, boot: int = 5000) -> dict:
    n = len(rs)
    if n == 0:
        return {"n": 0}
    wins, losses = rs[rs > 0], rs[rs <= 0]
    equity = np.cumsum(rs)
    dd = (np.maximum.accumulate(np.concatenate([[0], equity])) - np.concatenate([[0], equity])).max()
    rng = np.random.default_rng(seed)
    means = rng.choice(rs, size=(boot, n), replace=True).mean(axis=1)
    return {
        "n": n,
        "win_rate": float((rs > 0).mean()),
        "avg_r": float(rs.mean()),
        "ci95": (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))),
        "total_r": float(rs.sum()),
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
        "max_dd_r": float(dd),
    }


def fmt(m: dict) -> str:
    if m["n"] == 0:
        return "n=0"
    lo, hi = m["ci95"]
    return (f"n={m['n']:4d}  win={m['win_rate']:5.1%}  avgR={m['avg_r']:+.3f} [95%CI {lo:+.2f}..{hi:+.2f}]  "
            f"totalR={m['total_r']:+.1f}  PF={m['profit_factor']:.2f}  maxDD={m['max_dd_r']:.1f}R")


def random_baseline(data: dict, s: Settings, per_pair: int, seed: int = 11) -> np.ndarray:
    """Long acak dengan geometri risiko mirip (SL ~1.5 ATR, TP 2R): ekspektasi seharusnya ~ -fee."""
    rng = np.random.default_rng(seed)
    out = []
    for sym, (l, _, _, start_i) in data.items():
        a = atr(l).to_numpy()
        for i in rng.choice(np.arange(max(start_i, s.scan_window), len(l) - s.max_hold_candles), per_pair, replace=False):
            c = float(l["close"].iloc[i])
            plan, _ = plan_long(c, c - 1.4 * a[i], a[i], None, s)
            if plan is not None:
                out.append(simulate_trade(plan, l, int(i), s)[2])
    return np.array(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest lapisan harga+CVD (sisi Long)")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--pairs", type=int, default=20)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-prefilter", action="store_true", help="evaluasi semua candle (lambat; untuk verifikasi)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    s = SETTINGS

    rest = BinanceREST()
    symbols = rest.top_symbols(args.pairs, s.min_quote_volume_usd)
    warm_days = 5  # pemanasan sebelum periode uji
    data: dict[str, tuple] = {}
    for sym in symbols:
        l = load_history(rest, sym, "15m", args.days + warm_days, args.refresh)
        h1 = load_history(rest, sym, "1h", args.days + warm_days + 10, args.refresh)
        h4 = load_history(rest, sym, "4h", args.days + warm_days + 40, args.refresh)
        if len(l) < s.scan_window + 500:
            print(f"lewati {sym}: histori terlalu pendek ({len(l)} candle)")
            continue
        start_i = int(np.searchsorted(l["close_time"].to_numpy(), l["close_time"].iloc[-1] - args.days * 86_400_000))
        data[sym] = (l, h1, h4, start_i)
    print(f"{len(data)} pair, {args.days} hari: {', '.join(data)}\n")

    jobs = [(sym, l, h1, h4, st, s, not args.no_prefilter) for sym, (l, h1, h4, st) in data.items()]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(scan_symbol, jobs))
    print(f"scan selesai {time.time() - t0:.0f} detik")

    funnel: Counter = Counter()
    raw: list[tuple[Trade, int]] = []
    for sym, sigs, why in results:
        funnel += why
        l = data[sym][0]
        for item in sigs:
            sg = item.signal
            raw.append((to_trade(sym, sg.plan, sg.score, l, item.idx, s), item.idx))

    print("\nAlasan penolakan pada candle kandidat:")
    for k, v in funnel.most_common(10):
        print(f"  {v:7d}  {k}")

    taken = apply_portfolio_rules(raw, s)
    rs_all = np.array([tr.r for tr, _ in raw])
    rs = np.array([t.r for t in taken])
    days = args.days
    print(f"\n=== HASIL (periode {days} hari, {len(data)} pair, ~{len(taken)/days:.2f} sinyal/hari) ===")
    print("Semua sinyal         :", fmt(metrics(rs_all)))
    print("Setelah aturan harian:", fmt(metrics(rs)))
    if len(rs) >= 6:
        med = np.median([t.score for t in taken])
        hi = np.array([t.r for t in taken if t.score >= med]); lo = np.array([t.r for t in taken if t.score < med])
        print(f"  skor >= {med:.0f}      :", fmt(metrics(hi)))
        print(f"  skor <  {med:.0f}      :", fmt(metrics(lo)))
    oc = Counter(t.outcome for t in taken)
    print(f"  hasil: {dict(oc)}")
    base = random_baseline(data, s, per_pair=max(50, len(raw) * 3 // max(len(data), 1)))
    print("Baseline entry acak  :", fmt(metrics(base)))
    by_month = Counter()
    for t in taken:
        by_month[t.time.strftime("%Y-%m")] += t.r
    print("R per bulan          :", {k: round(v, 1) for k, v in sorted(by_month.items())})

    DATA_DIR.mkdir(exist_ok=True)
    pd.DataFrame([t.__dict__ for t in taken]).to_csv(DATA_DIR / "backtest_trades.csv", index=False)
    print(f"\ntrade disimpan: {DATA_DIR / 'backtest_trades.csv'}")
    print("Catatan: pair dipilih dari top volume SEKARANG (survivorship bias); lapisan posisi MCP tidak diuji; "
          "batas rugi harian belum dimodelkan.")


if __name__ == "__main__":
    main()
