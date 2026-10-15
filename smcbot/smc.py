"""Detektor Smart Money Concepts (sisi Long) di atas DataFrame klines.

Aturan anti-lookahead: swing di indeks i baru 'diketahui' di candle i+right.
Semua fungsi hanya memakai swing yang sudah terkonfirmasi pada saat evaluasi,
jadi hasil backtest dan hasil live konsisten.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# Dasar: ATR dan swing
# --------------------------------------------------------------------------
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def swing_points(df: pd.DataFrame, left: int = 3, right: int = 3) -> pd.DataFrame:
    """Pivot high/low. Kolom: idx, price, kind ('high'|'low'), confirmed_at."""
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    rows = []
    for i in range(left, len(df) - right):
        h_win, l_win = highs[i - left : i + right + 1], lows[i - left : i + right + 1]
        if highs[i] == h_win.max() and (h_win == highs[i]).sum() == 1:
            rows.append((i, highs[i], "high", i + right))
        if lows[i] == l_win.min() and (l_win == lows[i]).sum() == 1:
            rows.append((i, lows[i], "low", i + right))
    return pd.DataFrame(rows, columns=["idx", "price", "kind", "confirmed_at"])


def htf_bias(df: pd.DataFrame, left: int = 3, right: int = 3) -> int:
    """+1 bullish (HH & HL), -1 bearish (LH & LL), 0 netral/campuran."""
    sw = swing_points(df, left, right)
    highs = sw[sw["kind"] == "high"]["price"].to_numpy()
    lows = sw[sw["kind"] == "low"]["price"].to_numpy()
    if len(highs) < 2 or len(lows) < 2:
        return 0
    hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
    if hh and hl:
        return 1
    if not hh and not hl:
        return -1
    return 0


# --------------------------------------------------------------------------
# Liquidity sweep + reclaim (pemicu utama)
# --------------------------------------------------------------------------
def reclaim_ok(close: float, level: float, sweep_low: float) -> bool:
    """Apakah penutupan candle cukup meyakinkan sebagai 'reclaim' di atas level?

    Ini titik penilaian yang paling berpengaruh pada kualitas sinyal: terlalu
    longgar -> banyak fakeout; terlalu ketat -> sinyal terlambat/jarang.
    Default: close harus di atas level DAN menutup minimal 10% dari kedalaman
    sapuan di atas level (bukan sekadar menyentuh).
    """
    depth = level - sweep_low
    return close > level + 0.1 * depth


@dataclass(frozen=True)
class Sweep:
    level: float          # likuiditas (swing low) yang disapu
    sweep_low: float      # titik terendah sapuan -> dasar SL
    sweep_idx: int
    reclaim_idx: int
    depth_pct: float      # kedalaman sapuan relatif terhadap level
    lower_wick_ratio: float  # porsi ekor bawah pada candle sapuan (0..1)
    swing_idx: int = -1   # candle swing low asal (untuk membandingkan CVD)


def find_long_sweep(df: pd.DataFrame, swings: pd.DataFrame | None = None,
                    reclaim_window: int = 2, swing_lookback: int = 60) -> Sweep | None:
    """Sweep swing-low yang di-reclaim TEPAT pada candle terakhir (candle close terbaru).

    Syarat: level masih utuh (tidak ditembus sebelum sapuan) dan swing sudah
    terkonfirmasi sebelum candle sapuan.
    """
    n = len(df)
    if n < 20:
        return None
    swings = swing_points(df) if swings is None else swings
    lows_sw = swings[swings["kind"] == "low"]
    low, close, high = df["low"].to_numpy(), df["close"].to_numpy(), df["high"].to_numpy()
    last = n - 1

    best: Sweep | None = None
    for k in range(max(0, last - reclaim_window), last + 1):
        cands = lows_sw[(lows_sw["confirmed_at"] < k) & (lows_sw["idx"] >= k - swing_lookback)]
        for _, s in cands.iterrows():
            level, sidx = float(s["price"]), int(s["idx"])
            if low[k] >= level:
                continue
            if sidx + 1 < k and low[sidx + 1 : k].min() <= level:
                continue  # level sudah pernah ditembus -> bukan likuiditas utuh
            # reclaim harus terjadi di candle terakhir, dan belum ada reclaim lebih awal
            span = slice(k, last + 1)
            sweep_low = float(low[span].min())
            if not reclaim_ok(close[last], level, sweep_low):
                continue
            if k < last and any(reclaim_ok(c, level, sweep_low) for c in close[k:last]):
                continue  # reclaim sudah terjadi sebelumnya -> sinyal basi
            rng = high[k] - low[k]
            wick = (min(df["open"].iloc[k], close[k]) - low[k]) / rng if rng > 0 else 0.0
            cand = Sweep(level, sweep_low, k, last, (level - sweep_low) / level, float(wick), sidx)
            if best is None or cand.level > best.level:  # likuiditas terdekat di atas
                best = cand
    return best


# --------------------------------------------------------------------------
# Zona: Fair Value Gap dan Order Block (bullish)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Zone:
    kind: str     # 'FVG' | 'OB'
    bottom: float
    top: float
    idx: int      # candle pembentuk


def latest_bullish_fvg(df: pd.DataFrame, lookback: int = 80) -> Zone | None:
    """FVG bullish terbaru yang belum terisi penuh."""
    high, low = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    for i in range(n - 1, max(1, n - lookback), -1):
        if low[i] > high[i - 2]:
            bottom, top = float(high[i - 2]), float(low[i])
            if i + 1 >= n or low[i + 1 :].min() > bottom:
                return Zone("FVG", bottom, top, i)
    return None


def latest_bullish_ob(df: pd.DataFrame, lookback: int = 80, disp_mult: float = 1.5) -> Zone | None:
    """Order block bullish: candle bearish terakhir sebelum candle displacement naik
    yang menembus high 10 candle sebelumnya. Zona hilang bila harga menutup di bawahnya."""
    a = atr(df).to_numpy()
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    n = len(df)
    for i in range(n - 1, max(11, n - lookback), -1):
        if np.isnan(a[i]) or c[i] <= o[i] or (c[i] - o[i]) < disp_mult * a[i]:
            continue
        if c[i] <= h[i - 10 : i].max():
            continue
        for j in range(i - 1, max(i - 6, 0), -1):
            if c[j] < o[j]:
                if i + 1 >= n or c[i + 1 :].min() >= l[j]:
                    return Zone("OB", float(l[j]), float(h[j]), j)
                break
    return None


def price_in_zone(zone: Zone | None, low: float, close: float, tolerance: float = 0.0) -> bool:
    """Candle terakhir menyentuh zona (low masuk zona) dan tidak menutup di bawahnya."""
    if zone is None:
        return False
    return low <= zone.top * (1 + tolerance) and close >= zone.bottom
