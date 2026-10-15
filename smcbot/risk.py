"""SL/TP dan ukuran posisi. Semua angka sudah memperhitungkan fee taker dua sisi."""
from __future__ import annotations

from dataclasses import dataclass

from .config import SETTINGS, Settings


@dataclass(frozen=True)
class TradePlan:
    entry: float
    sl: float
    tp: float
    risk_pct: float      # jarak entry ke SL (%)
    gross_rr: float
    net_rr: float        # setelah fee
    qty: float
    notional: float
    leverage: float
    risk_usd: float      # kerugian bila SL kena, sudah termasuk fee


def plan_long(entry: float, sweep_low: float, atr_value: float,
              swing_high_above: float | None, s: Settings = SETTINGS) -> tuple[TradePlan | None, str]:
    """Kembalikan (rencana, '') atau (None, alasan penolakan)."""
    sl = sweep_low - s.sl_buffer_atr * atr_value
    risk = entry - sl
    if risk <= 0:
        return None, "SL tidak valid (di atas entry)"

    cap = entry + s.tp_max_r * risk
    tp = min(swing_high_above, cap) if swing_high_above and swing_high_above > entry else entry + 2 * risk
    reward = tp - entry

    fee = entry * s.taker_fee_pct / 100 * 2  # masuk + keluar, per unit
    net_rr = (reward - fee) / (risk + fee)
    if net_rr < s.min_net_rr:
        return None, f"RR bersih {net_rr:.2f} < {s.min_net_rr}"

    qty = s.risk_usd / (risk + fee)
    notional = qty * entry
    leverage = notional / s.account_usd
    if leverage > s.max_leverage:
        return None, f"leverage {leverage:.1f}x > {s.max_leverage}x (SL terlalu sempit)"

    return TradePlan(
        entry=entry, sl=sl, tp=tp, risk_pct=risk / entry * 100,
        gross_rr=reward / risk, net_rr=net_rr, qty=qty, notional=notional,
        leverage=leverage, risk_usd=qty * (risk + fee),
    ), ""
