"""Order flow dari klines: CVD dan deteksi absorpsi (tanpa panggilan API tambahan)."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .smc import Sweep


def delta(df: pd.DataFrame) -> pd.Series:
    """Volume beli agresif dikurangi jual agresif per candle (taker buy - taker sell)."""
    return 2 * df["taker_buy_base"] - df["volume"]


def cvd(df: pd.DataFrame) -> pd.Series:
    return delta(df).cumsum()


@dataclass(frozen=True)
class Absorption:
    cvd_higher_low: bool     # harga membuat low lebih rendah, CVD tidak
    reclaim_delta_pos: bool  # candle reclaim didominasi pembeli agresif
    buy_ratio_3: float       # porsi taker buy 3 candle terakhir (0..1)


def absorption(df: pd.DataFrame, sweep: Sweep) -> Absorption:
    c = cvd(df).to_numpy()
    d = delta(df).to_numpy()
    s = max(sweep.swing_idx, 0)
    swing_cvd_low = c[max(s - 1, 0) : s + 2].min()
    sweep_cvd_low = c[sweep.sweep_idx : sweep.reclaim_idx + 1].min()
    last3 = df.iloc[-3:]
    vol = last3["volume"].sum()
    return Absorption(
        cvd_higher_low=bool(sweep_cvd_low > swing_cvd_low),
        reclaim_delta_pos=bool(d[sweep.reclaim_idx] > 0),
        buy_ratio_3=float(last3["taker_buy_base"].sum() / vol) if vol > 0 else 0.5,
    )
