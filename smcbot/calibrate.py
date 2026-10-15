"""Kalibrasi skor: catat SEMUA kandidat yang lolos gerbang + hasil simulasinya, lalu analisis
dengan split waktu (latih = paruh awal, uji = paruh akhir, disentuh sekali).

  python -m smcbot.calibrate scan --days 180 --pairs 20     # lambat, hasil ke data/candidates.csv
  python -m smcbot.calibrate analyze                        # cepat, bisa diulang
"""
from __future__ import annotations

import argparse
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from .backtest import DATA_DIR, HTF_TAIL, _htf_window, candidate_mask, load_history, metrics, fmt, simulate_trade
from .binance_rest import BinanceREST
from .config import SETTINGS, Settings
from .scorer import gather_candidate

CSV = DATA_DIR / "candidates.csv"


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------
def scan_symbol(args) -> list[dict]:
    symbol, l, h1, h4, start_i, s = args
    mask = candidate_mask(l)
    h1c, h4c = h1["close_time"].to_numpy(), h4["close_time"].to_numpy()
    rows = []
    for i in range(max(start_i, s.scan_window), len(l)):
        if not mask[i]:
            continue
        w = l.iloc[i - s.scan_window + 1 : i + 1].reset_index(drop=True)
        t = int(w["close_time"].iloc[-1])
        c, _ = gather_candidate(symbol, w, _htf_window(h1, h1c, t), _htf_window(h4, h4c, t), s)
        if c is None or c.plan is None:
            continue
        outcome, j, r = simulate_trade(c.plan, l, i, s)
        f = c.flow
        rows.append({
            "symbol": symbol, "idx": i, "time": l["time"].iloc[i], "exit_time": l["time"].iloc[j],
            "score": c.price_score, "s_sweep": c.breakdown["sweep"], "s_zona": c.breakdown["zona"],
            "s_flow": c.breakdown["flow"],
            "wick": c.sweep.lower_wick_ratio, "depth_atr": c.depth_atr, "zone_tf": c.zone_tf,
            "zone_kind": c.zone.kind, "b1": c.bias_1h, "b4": c.bias_4h,
            "cvd_hl": f.cvd_higher_low, "delta_pos": f.reclaim_delta_pos, "buy3": f.buy_ratio_3,
            "risk_pct": c.plan.risk_pct, "net_rr": c.plan.net_rr, "outcome": outcome, "r": r,
        })
    return rows


def run_scan(days: int, pairs: int, workers: int) -> None:
    s, rest = SETTINGS, BinanceREST()
    symbols = rest.top_symbols(pairs, s.min_quote_volume_usd)
    jobs = []
    for sym in symbols:
        l = load_history(rest, sym, "15m", days + 5)
        h1 = load_history(rest, sym, "1h", days + 15)
        h4 = load_history(rest, sym, "4h", days + 45)
        if len(l) < s.scan_window + 500:
            continue
        st = int(np.searchsorted(l["close_time"].to_numpy(), l["close_time"].iloc[-1] - days * 86_400_000))
        jobs.append((sym, l, h1, h4, st, s))
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        out = [r for rows in ex.map(scan_symbol, jobs) for r in rows]
    df = pd.DataFrame(out).sort_values("time").reset_index(drop=True)
    DATA_DIR.mkdir(exist_ok=True)
    df.to_csv(CSV, index=False)
    print(f"{len(jobs)} pair, {len(df)} kandidat (lolos gerbang + rencana valid), {time.time() - t0:.0f} detik -> {CSV}")


# --------------------------------------------------------------------------
# Analisis
# --------------------------------------------------------------------------
def dedupe_non_overlapping(df: pd.DataFrame) -> pd.DataFrame:
    """Satu posisi per pair pada satu waktu, seperti trading nyata (kandidat beruntun = 1 trade)."""
    keep, busy = [], {}
    for k, row in df.sort_values("time").iterrows():
        if busy.get(row["symbol"], pd.Timestamp.min.tz_localize("UTC")) > row["time"]:
            continue
        busy[row["symbol"]] = row["exit_time"]
        keep.append(k)
    return df.loc[keep]


def _m(df: pd.DataFrame) -> str:
    return fmt(metrics(df["r"].to_numpy()))


def analyze(min_train: int = 40) -> None:
    df = pd.read_csv(CSV, parse_dates=["time", "exit_time"])
    print(f"kandidat mentah: {len(df)}")
    df = dedupe_non_overlapping(df)
    cut = df["time"].min() + (df["time"].max() - df["time"].min()) / 2
    tr, te = df[df["time"] < cut], df[df["time"] >= cut]
    print(f"setelah satu-posisi-per-pair: {len(df)} trade | latih: {len(tr)} (< {cut:%Y-%m-%d}) | uji: {len(te)}\n")

    print("== 1. Semua kandidat lolos gerbang, tanpa ambang skor (baseline gerbang) ==")
    print("  latih :", _m(tr)); print("  uji   :", _m(te))
    print("  (baseline entry acak dari backtest sebelumnya: avgR -0.19)\n")

    print("== 2. Apakah skor informatif? Korelasi Spearman skor vs R (tanpa tuning) ==")
    for name, d in (("latih", tr), ("uji", te)):
        rho = d["score"].corr(d["r"], method="spearman")
        print(f"  {name}: rho={rho:+.3f} (n={len(d)}; |rho|<~{2 / np.sqrt(len(d)):.2f} = tidak bisa dibedakan dari noise)")
    print()

    print("== 3. R per kuartil skor (tepi kuartil dari data LATIH) ==")
    edges = np.unique(np.quantile(tr["score"], [0, .25, .5, .75, 1]))
    for name, d in (("latih", tr), ("uji", te)):
        b = pd.cut(d["score"], bins=np.concatenate([[-np.inf], edges[1:-1], [np.inf]]), include_lowest=True)
        for iv, g in d.groupby(b, observed=True):
            print(f"  {name} skor {str(iv):>16s}: {_m(g)}")
    print()

    print("== 4. Faktor biner: dipilih/dipelajari dari LATIH, diperiksa di UJI ==")
    feats = {
        "CVD higher-low": lambda d: d["cvd_hl"],
        "delta reclaim positif": lambda d: d["delta_pos"],
        "taker buy 3c >= 55%": lambda d: d["buy3"] >= 0.55,
        "zona 1H (vs 15m)": lambda d: d["zone_tf"] == "1h",
        "bias 1H & 4H bullish": lambda d: (d["b1"] == 1) & (d["b4"] == 1),
        "ekor bawah >= 40%": lambda d: d["wick"] >= 0.4,
        "sapuan 0.5-1.5 ATR": lambda d: d["depth_atr"].between(0.5, 1.5),
    }
    print(f"  {'faktor':26s} | {'LATIH ya':>24s} | {'LATIH tidak':>24s} | {'UJI ya':>24s}")
    for name, fn in feats.items():
        a_tr, b_tr, a_te = tr[fn(tr)], tr[~fn(tr)], te[fn(te)]
        cell = lambda g: f"n={len(g):4d} avgR={g['r'].mean():+.2f}" if len(g) else "n=0"
        print(f"  {name:26s} | {cell(a_tr):>24s} | {cell(b_tr):>24s} | {cell(a_te):>24s}")
    print()

    print("== 5. Ambang skor: dipilih HANYA dari LATIH (n >= %d), dipakai SEKALI di UJI ==" % min_train)
    best = None
    for th in np.unique(np.round(np.quantile(tr["score"], np.linspace(0, .9, 19)), 1)):
        g = tr[tr["score"] >= th]
        if len(g) >= min_train and (best is None or g["r"].mean() > best[1]):
            best = (th, g["r"].mean(), len(g))
    if best is None:
        print("  tidak ada ambang dengan sampel cukup"); return
    th = best[0]
    print(f"  ambang terbaik di latih: skor >= {th} (avgR {best[1]:+.3f}, n={best[2]}) <- optimistis karena dipilih dari data yang sama")
    print("  latih :", _m(tr[tr["score"] >= th]))
    print("  UJI   :", _m(te[te["score"] >= th]))
    print("  UJI semua (tanpa ambang):", _m(te))
    print("\nCatatan: tiap analisis di atas adalah perbandingan ganda pada sampel kecil; hanya hasil UJI yang punya arti,")
    print("dan hanya jika CI 95%-nya tidak mencakup nol DAN lebih baik dari baseline.")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan"); sc.add_argument("--days", type=int, default=180)
    sc.add_argument("--pairs", type=int, default=20); sc.add_argument("--workers", type=int, default=4)
    sub.add_parser("analyze")
    a = ap.parse_args()
    run_scan(a.days, a.pairs, a.workers) if a.cmd == "scan" else analyze()


if __name__ == "__main__":
    main()
