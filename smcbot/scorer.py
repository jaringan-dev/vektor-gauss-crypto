"""Confluence scorer: gerbang keras -> skor harga -> (MCP) skor posisi -> rencana trade.

Skor maksimum 100:  sweep 25 + zona 15 + CVD 25  = 65 (murni harga/flow, tanpa API tambahan)
                    posisi (funding, retail, top trader, OI) = 35 (dari MCP)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from .config import SETTINGS, Settings
from .flow import Absorption, absorption
from .positioning import Positioning
from .risk import TradePlan, plan_long
from .smc import (
    Sweep, Zone, atr, find_long_sweep, htf_bias, latest_bullish_fvg,
    latest_bullish_ob, price_in_zone, swing_points,
)


@dataclass(frozen=True)
class Signal:
    symbol: str
    score: float
    breakdown: dict[str, float]
    reasons: list[str]
    plan: TradePlan
    sweep: Sweep
    zone: Zone | None
    positioning: Positioning | None
    degraded: bool            # True bila lapisan MCP tidak tersedia
    time: pd.Timestamp = field(default=None)


# --------------------------------------------------------------------------
# Komponen skor
# --------------------------------------------------------------------------
def score_sweep(sw: Sweep, atr_value: float, bias_1h: int, bias_4h: int) -> tuple[float, list[str]]:
    pts, why = 0.0, []
    wick = min(sw.lower_wick_ratio / 0.6, 1.0) * 12          # ekor bawah panjang = penolakan kuat
    pts += wick
    if sw.lower_wick_ratio >= 0.4:
        why.append(f"ekor bawah {sw.lower_wick_ratio:.0%} dari candle sapuan")
    depth_atr = (sw.level - sw.sweep_low) / atr_value
    if 0.5 <= depth_atr <= 1.5:                               # sapuan 'sehat': cukup dalam, tidak jebol
        pts += 8
        why.append(f"sapuan {depth_atr:.1f}×ATR di bawah likuiditas")
    elif depth_atr >= 0.3:
        pts += 4
    if bias_1h == 1 and bias_4h == 1:
        pts += 5
        why.append("bias 1H & 4H bullish")
    elif bias_1h == 1 or bias_4h == 1:
        pts += 2
    return pts, why


def score_zone(zone: Zone | None, tf: str) -> tuple[float, list[str]]:
    if zone is None:
        return 0.0, []
    pts = 15.0 if tf != "15m" else 10.0
    return pts, [f"sapuan terjadi di {zone.kind} {tf} ({zone.bottom:.6g}-{zone.top:.6g})"]


def score_flow(df: pd.DataFrame, sw: Sweep, a: Absorption | None = None) -> tuple[float, list[str]]:
    a = a or absorption(df, sw)
    pts, why = 0.0, []
    if a.cvd_higher_low:
        pts += 15
        why.append("CVD higher-low: harga turun tapi penjual agresif melemah (absorpsi)")
    if a.reclaim_delta_pos:
        pts += 5
        why.append("candle reclaim didominasi pembeli agresif")
    if a.buy_ratio_3 >= 0.55:
        pts += 5
        why.append(f"taker buy {a.buy_ratio_3:.0%} dalam 3 candle terakhir")
    return pts, why


def score_positioning(p: Positioning) -> tuple[float, list[str]]:
    """Seberapa padat retail di sisi yang salah, dan ke mana top trader condong. Maks 35."""
    pts, why = 0.0, []
    if p.funding_rate < 0:
        pts += 15 if p.funding_rate <= -0.0001 else 8
        why.append(f"funding negatif ({p.funding_rate * 100:.4f}%): short membayar long")
    if p.retail_long_pct < 50:
        pts += 8
        why.append(f"retail condong short ({100 - p.retail_long_pct:.0f}% akun short)")
    if p.top_long_pct - p.retail_long_pct >= 5:
        pts += 8
        why.append(f"top trader {p.top_long_pct:.0f}% long vs retail {p.retail_long_pct:.0f}%")
    if p.oi_change_pct < 0:
        pts += 4
        why.append(f"OI turun {p.oi_change_pct:.2f}% (posisi tersapu)")
    return pts, why


# --------------------------------------------------------------------------
# Kandidat: semua yang lolos gerbang, SEBELUM ambang skor diterapkan
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    symbol: str
    sweep: Sweep
    zone: Zone
    zone_tf: str
    bias_1h: int
    bias_4h: int
    depth_atr: float
    breakdown: dict[str, float]    # sweep / zona / flow
    reasons: list[str]
    flow: Absorption
    plan: TradePlan | None         # None bila ditolak risk (RR bersih/leverage)
    plan_reason: str
    time: pd.Timestamp

    @property
    def price_score(self) -> float:
        return float(sum(self.breakdown.values()))


def _make_plan(ltf: pd.DataFrame, sw: Sweep, atr_value: float, s: Settings) -> tuple[TradePlan | None, str]:
    """TP ke likuiditas terdekat di atas (swing high), dibatasi tp_max_r."""
    close = float(ltf["close"].iloc[-1])
    swings = swing_points(ltf)
    last = len(ltf) - 1
    highs = swings[(swings["kind"] == "high") & (swings["confirmed_at"] <= last) & (swings["price"] > close)]
    nearest = float(highs["price"].min()) if len(highs) else None
    return plan_long(close, sw.sweep_low, atr_value, nearest, s)


def gather_candidate(
    symbol: str, ltf: pd.DataFrame, htf1: pd.DataFrame, htf4: pd.DataFrame, s: Settings = SETTINGS,
) -> tuple[Candidate | None, str]:
    """Jalankan gerbang keras + hitung skor harga. Kembalikan (Candidate, '') atau (None, alasan)."""
    sw = find_long_sweep(ltf)
    if sw is None:
        return None, "tidak ada sweep+reclaim"

    a = atr(ltf).iloc[-1]
    if pd.isna(a) or a <= 0:
        return None, "ATR belum tersedia"
    depth_atr = (sw.level - sw.sweep_low) / a
    if depth_atr < s.min_sweep_atr:
        return None, f"sapuan terlalu dangkal ({depth_atr:.2f}×ATR)"
    if depth_atr > s.max_sweep_atr:
        return None, f"sapuan terlalu dalam ({depth_atr:.2f}×ATR), mirip breakdown"

    # Gerbang 1: bias HTF tidak boleh bearish
    b1, b4 = htf_bias(htf1), htf_bias(htf4)
    if b1 == -1 or b4 == -1:
        return None, f"bias HTF bearish (1H={b1}, 4H={b4})"

    # Gerbang 2: sapuan harus terjadi di zona demand (OB/FVG) 15m atau 1H
    close, sweep_low = float(ltf["close"].iloc[-1]), sw.sweep_low
    options = [
        (latest_bullish_ob(htf1), "1h"), (latest_bullish_fvg(htf1), "1h"),
        (latest_bullish_ob(ltf), "15m"), (latest_bullish_fvg(ltf), "15m"),
    ]
    zone, zone_tf = next(((z, tf) for z, tf in options if price_in_zone(z, sweep_low, close)), (None, ""))
    if zone is None:
        return None, "sapuan tidak berada di zona OB/FVG"

    flow = absorption(ltf, sw)
    breakdown: dict[str, float] = {}
    reasons: list[str] = []
    for name, (pts, why) in {
        "sweep": score_sweep(sw, a, b1, b4),
        "zona": score_zone(zone, zone_tf),
        "flow": score_flow(ltf, sw, flow),
    }.items():
        breakdown[name] = round(pts, 1)
        reasons += why

    plan, plan_reason = _make_plan(ltf, sw, float(a), s)
    return Candidate(symbol, sw, zone, zone_tf, b1, b4, float(depth_atr), breakdown, reasons, flow,
                     plan, plan_reason, ltf["time"].iloc[-1]), ""


# --------------------------------------------------------------------------
# Evaluasi satu pair (kandidat + ambang skor + data posisi)
# --------------------------------------------------------------------------
def evaluate(
    symbol: str,
    ltf: pd.DataFrame,
    htf1: pd.DataFrame,
    htf4: pd.DataFrame,
    fetch_positioning: Callable[[str], Positioning | None] | None = None,
    s: Settings = SETTINGS,
) -> tuple[Signal | None, str]:
    """Kembalikan (Signal, '') atau (None, alasan). Alasan dipakai untuk log dan tuning."""
    cand, why = gather_candidate(symbol, ltf, htf1, htf4, s)
    if cand is None:
        return None, why

    breakdown, reasons = dict(cand.breakdown), list(cand.reasons)
    price_score = cand.price_score
    if price_score < s.prefilter_score:
        return None, f"skor harga {price_score:.0f} < prefilter {s.prefilter_score:.0f}"

    # Lapisan posisi (MCP) hanya untuk kandidat yang lolos prefilter
    pos = fetch_positioning(symbol) if fetch_positioning else None
    degraded = pos is None
    if pos is not None:
        pts, pwhy = score_positioning(pos)
        breakdown["posisi"] = round(pts, 1)
        reasons += pwhy
    total = sum(breakdown.values())
    threshold = s.score_threshold_degraded if degraded else s.score_threshold
    if total < threshold:
        return None, f"skor {total:.0f} < ambang {threshold:.0f}" + (" (tanpa data posisi)" if degraded else "")

    if cand.plan is None:
        return None, f"rencana ditolak: {cand.plan_reason}"

    return Signal(
        symbol=symbol, score=round(total, 1), breakdown=breakdown, reasons=reasons, plan=cand.plan,
        sweep=cand.sweep, zone=cand.zone, positioning=pos, degraded=degraded, time=cand.time,
    ), ""
