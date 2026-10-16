"""Kalibrasi skor: catat SEMUA kandidat yang lolos gerbang + hasil simulasinya, lalu analisis
dengan split waktu (latih = paruh awal, uji = paruh akhir, disentuh sekali).

  python -m smcbot.calibrate scan --days 180 --pairs 20     # lambat, hasil ke data/candidates.csv
  python -m smcbot.calibrate analyze                        # cepat, bisa diulang
  python -m smcbot.calibrate scan --offset-days 180         # hold-out: 180 hari SEBELUM periode kalibrasi
  python -m smcbot.calibrate holdout                        # vonis tunggal, aturan dibekukan
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

import numpy as np
import pandas as pd

from .backtest import DATA_DIR, HTF_TAIL, _htf_window, candidate_mask, load_history, metrics, fmt, simulate_trade
from .binance_rest import BinanceREST
from .config import SETTINGS, Settings
from .scorer import gather_candidate

CSV = DATA_DIR / "candidates.csv"
CSV_HOLD = DATA_DIR / "candidates_holdout.csv"

#: ATURAN DIBEKUKAN dari kalibrasi 2026-10-16 (dipilih dari data latih saja). Jangan diubah untuk hold-out.
FROZEN_THRESHOLD = 37.0


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------
def scan_symbol(args) -> list[dict]:
    symbol, l, h1, h4, start_i, s, *rest = args
    end_i = rest[0] if rest else len(l)
    mask = candidate_mask(l)
    h1c, h4c = h1["close_time"].to_numpy(), h4["close_time"].to_numpy()
    rows = []
    for i in range(max(start_i, s.scan_window), min(end_i, len(l))):
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


def _prefix(offset: int) -> str:
    return "cand" if offset == 0 else "hold"


def scan_one(symbol: str, days: int, offset: int = 0) -> None:
    """Pindai satu pair pada jendela [-(offset+days), -offset] hari dan tulis data/<prefix>_<SYMBOL>.csv."""
    s, rest = SETTINGS, BinanceREST()
    total = offset + days
    l = load_history(rest, symbol, "15m", total + 5)
    h1 = load_history(rest, symbol, "1h", total + 15)
    h4 = load_history(rest, symbol, "4h", total + 45)
    ct, last = l["close_time"].to_numpy(), int(l["close_time"].iloc[-1])
    end_ts = last - offset * 86_400_000
    st = int(np.searchsorted(ct, end_ts - days * 86_400_000))
    en = int(np.searchsorted(ct, end_ts))
    rows = scan_symbol((symbol, l, h1, h4, st, s, en))
    DATA_DIR.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(DATA_DIR / f"{_prefix(offset)}_{symbol}.csv", index=False)


def run_scan(days: int, pairs: int, workers: int, offset: int = 0) -> None:
    """Subproses terpisah per pair (bukan ProcessPoolExecutor: pool pecah dengan WinError 6 di Windows
    saat berjalan di latar belakang). stdin ditutup agar tidak mewarisi handle yang tidak valid."""
    s, rest = SETTINGS, BinanceREST()
    pre, out_csv = _prefix(offset), (CSV if offset == 0 else CSV_HOLD)
    if offset == 0:
        symbols = rest.top_symbols(pairs, s.min_quote_volume_usd)
    else:  # hold-out: daftar pair DIBEKUKAN dari kalibrasi, bukan top volume baru
        symbols = list(pd.read_csv(CSV)["symbol"].unique())
    DATA_DIR.mkdir(exist_ok=True)
    for sym in symbols:  # unduh/cek cache di proses induk agar subproses hanya membaca
        for tf, extra in (("15m", 5), ("1h", 15), ("4h", 45)):
            load_history(rest, sym, tf, offset + days + extra)
    t0 = time.time()
    pending, running, done, failed = list(symbols), {}, 0, []
    while pending or running:
        while pending and len(running) < workers:
            sym = pending.pop(0)
            (DATA_DIR / f"{pre}_{sym}.csv").unlink(missing_ok=True)
            running[sym] = subprocess.Popen(
                [sys.executable, "-m", "smcbot.calibrate", "scan-one", sym, "--days", str(days), "--offset-days", str(offset)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        time.sleep(2)
        for sym, proc in list(running.items()):
            if proc.poll() is None:
                continue
            del running[sym]
            done += 1
            f = DATA_DIR / f"{pre}_{sym}.csv"
            ok = proc.returncode == 0 and f.exists()
            if not ok:
                failed.append(sym)
            n = len(pd.read_csv(f)) if ok and f.stat().st_size > 5 else 0
            print(f"[{done}/{len(symbols)}] {sym}: {'GAGAL' if not ok else str(n) + ' kandidat'} ({time.time() - t0:.0f} detik)", flush=True)
    frames = [pd.read_csv(DATA_DIR / f"{pre}_{x}.csv", parse_dates=["time", "exit_time"])
              for x in symbols if x not in failed and (DATA_DIR / f"{pre}_{x}.csv").stat().st_size > 5]
    df = pd.concat(frames, ignore_index=True).sort_values("time").reset_index(drop=True)
    df.to_csv(out_csv, index=False)
    print(f"{len(symbols) - len(failed)}/{len(symbols)} pair, {len(df)} kandidat, {time.time() - t0:.0f} detik -> {out_csv}"
          + (f" | GAGAL: {failed}" if failed else ""))


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
    verdict(tr, te, th, n_pairs=df["symbol"].nunique())
    print("\nCatatan: tiap analisis di atas adalah perbandingan ganda pada sampel kecil; hanya hasil UJI yang punya arti,")
    print("dan hanya jika CI 95%-nya tidak mencakup nol DAN lebih baik dari baseline.")


#: Kriteria sesuai tujuan aplikasi, DITETAPKAN SEBELUM hasil kalibrasi dilihat (2026-10-15).
#: Tujuan: sinyal Long berprobabilitas tinggi, 1-5 per hari, eksekusi manual, risiko $5.
GOAL = {
    "win_margin": 0.05,         # win rate harus >= titik impas + 5 poin persen
    "min_signals_per_day": 1.0,  # diproyeksikan ke 75 pair
    "max_signals_per_day": 5.0,
    "min_test_trades": 30,      # di bawah ini tidak ada kesimpulan
}


def verdict(tr: pd.DataFrame, te: pd.DataFrame, th: float, n_pairs: int, universe: int = 75) -> None:
    """Vonis terhadap tujuan aplikasi, pada data UJI dengan ambang yang dipilih dari LATIH."""
    sel = te[te["score"] >= th].sort_values("time")
    print("\n== 6. VONIS terhadap tujuan aplikasi (data UJI, ambang dari LATIH) ==")
    if len(sel) < GOAL["min_test_trades"]:
        print(f"  TIDAK ADA KESIMPULAN: hanya {len(sel)} trade uji (< {GOAL['min_test_trades']}).")
        return
    m = metrics(sel["r"].to_numpy())
    win_r = sel.loc[sel["r"] > 0, "r"].mean()
    loss_r = -sel.loc[sel["r"] <= 0, "r"].mean()
    breakeven = loss_r / (win_r + loss_r) if win_r + loss_r > 0 else float("nan")
    days = max((sel["time"].max() - sel["time"].min()).days, 1)
    per_day = len(sel) / days * universe / n_pairs
    halves = [sel.iloc[: len(sel) // 2], sel.iloc[len(sel) // 2 :]]
    checks = {
        f"win rate {m['win_rate']:.1%} >= impas {breakeven:.1%} + {GOAL['win_margin']:.0%}":
            m["win_rate"] >= breakeven + GOAL["win_margin"],
        f"rata-rata R {m['avg_r']:+.3f} > 0": m["avg_r"] > 0,
        f"positif di kedua paruh uji ({halves[0]['r'].mean():+.2f} / {halves[1]['r'].mean():+.2f})":
            all(h["r"].mean() > 0 for h in halves),
        f"frekuensi ~{per_day:.1f}/hari (proyeksi {universe} pair) dalam {GOAL['min_signals_per_day']:.0f}-{GOAL['max_signals_per_day']:.0f}":
            GOAL["min_signals_per_day"] <= per_day <= GOAL["max_signals_per_day"],
    }
    for text, ok in checks.items():
        print(f"  [{'OK' if ok else 'GAGAL'}] {text}")
    proven = m["ci95"][0] > 0
    print(f"  [{'OK' if proven else 'BELUM'}] CI 95% rata-rata R: {m['ci95'][0]:+.2f}..{m['ci95'][1]:+.2f} "
          f"({'tidak mencakup nol' if proven else 'mencakup nol: belum terbukti'})")
    if all(checks.values()) and proven:
        print("  VONIS: LULUS. Lanjut paper forward-test.")
    elif all(checks.values()):
        print("  VONIS: MENJANJIKAN tetapi belum terbukti. Boleh paper forward-test, BUKAN uang sungguhan.")
    else:
        print("  VONIS: BELUM MEMENUHI tujuan. Jangan dilanjutkan ke uang sungguhan; revisi hipotesis, bukan ambang.")


def _btc_move(days: int, offset: int) -> float | None:
    """Perubahan harga BTC (%) pada jendela [-(offset+days), -offset] hari, dari cache. Konteks rezim saja."""
    f = DATA_DIR / f"BTCUSDT_15m_{offset + days + 5}d.pkl"
    if not f.exists():
        f = DATA_DIR / f"BTCUSDT_15m_{days + 5}d.pkl"
        if offset or not f.exists():
            return None
    b = pd.read_pickle(f)
    end_ts = int(b["close_time"].iloc[-1]) - offset * 86_400_000
    a, z = np.searchsorted(b["close_time"].to_numpy(), [end_ts - days * 86_400_000, end_ts])
    return float((b["close"].iloc[min(z, len(b) - 1)] / b["close"].iloc[a] - 1) * 100)


def holdout(days: int = 180) -> None:
    """Vonis TUNGGAL pada 180 hari sebelum kalibrasi, dengan aturan dibekukan (skor >= FROZEN_THRESHOLD)."""
    raw = pd.read_csv(CSV_HOLD, parse_dates=["time", "exit_time"])
    df = dedupe_non_overlapping(raw)
    n_pairs = df["symbol"].nunique()
    print(f"HOLD-OUT: {len(raw)} kandidat mentah -> {len(df)} trade (satu-posisi-per-pair), {n_pairs} pair punya kandidat")
    print(f"aturan dibekukan: skor harga >= {FROZEN_THRESHOLD} (tanpa penyesuaian)")
    for name, off in (("kalibrasi (0..180 hari lalu)", 0), ("hold-out (180..360 hari lalu)", days)):
        mv = _btc_move(days, off)
        print(f"  konteks BTC {name}: {mv:+.1f}%" if mv is not None else f"  konteks BTC {name}: n/a")
    print("\n== Baseline gerbang, semua kandidat tanpa ambang ==\n ", _m(df))
    rho = df["score"].corr(df["r"], method="spearman")
    print(f"== Spearman skor vs R: rho={rho:+.3f} (n={len(df)}; noise < ~{2 / np.sqrt(len(df)):.2f})")
    sel = df[df["score"] >= FROZEN_THRESHOLD]
    print(f"== Aturan dibekukan (skor >= {FROZEN_THRESHOLD}) ==\n ", _m(sel))
    print("   (kalibrasi uji sebelumnya: n=41 win 43.9% avgR +0.245)")
    verdict(df.iloc[0:0], df, FROZEN_THRESHOLD, n_pairs=n_pairs)
    print("\nHold-out ini dinilai SEKALI. Bila gagal, jangan menyetel ulang ambang pada data ini.")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan"); sc.add_argument("--days", type=int, default=180)
    sc.add_argument("--pairs", type=int, default=20); sc.add_argument("--workers", type=int, default=4)
    sc.add_argument("--offset-days", type=int, default=0)
    so = sub.add_parser("scan-one"); so.add_argument("symbol"); so.add_argument("--days", type=int, default=180); so.add_argument("--offset-days", type=int, default=0)
    sub.add_parser("analyze")
    sub.add_parser("holdout")
    a = ap.parse_args()
    if a.cmd == "scan":
        run_scan(a.days, a.pairs, a.workers, a.offset_days)
    elif a.cmd == "scan-one":
        scan_one(a.symbol, a.days, a.offset_days)
    elif a.cmd == "holdout":
        holdout()
    else:
        analyze()


if __name__ == "__main__":
    main()
