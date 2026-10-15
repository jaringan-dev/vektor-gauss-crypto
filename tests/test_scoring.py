"""Tes risk, flow, dan scorer (deterministik, tanpa internet)."""
import pandas as pd
import pytest

from smcbot.config import Settings
from smcbot.flow import absorption, cvd
from smcbot.positioning import Positioning
from smcbot.risk import plan_long
from smcbot.scorer import evaluate, score_positioning
from smcbot.smc import Sweep

S = Settings()  # default: akun $500, risiko 1% = $5, fee 0.05%/sisi


# ---------------------------- risk -----------------------------------------
def test_plan_long_risk_is_exactly_five_dollars_including_fees():
    plan, why = plan_long(entry=100, sweep_low=98, atr_value=1, swing_high_above=106, s=S)
    assert plan is not None, why
    assert plan.sl == pytest.approx(97.9)
    assert plan.tp == 106                      # likuiditas terdekat, di bawah batas 3R
    assert plan.risk_usd == pytest.approx(5.0)  # loss di SL = $5 termasuk fee
    assert plan.net_rr < plan.gross_rr          # fee mengurangi RR
    assert plan.leverage == pytest.approx(plan.notional / 500)


def test_plan_long_caps_tp_at_max_r():
    plan, _ = plan_long(100, 98, 1, swing_high_above=130, s=S)
    assert plan.tp == pytest.approx(100 + 3 * 2.1)


def test_plan_long_defaults_to_2r_when_no_swing_high():
    plan, _ = plan_long(100, 98, 1, swing_high_above=None, s=S)
    assert plan.tp == pytest.approx(100 + 2 * 2.1)


def test_plan_long_rejects_when_fees_eat_the_reward():
    plan, why = plan_long(100, 99.9, 0.01, None, s=S)
    assert plan is None and "RR bersih" in why


def test_plan_long_rejects_excess_leverage():
    tight = Settings(max_leverage=0.3)
    plan, why = plan_long(100, 98, 1, 106, s=tight)
    assert plan is None and "leverage" in why


# ---------------------------- flow -----------------------------------------
def _flow_df(buys, vols):
    df = pd.DataFrame({"taker_buy_base": buys, "volume": vols})
    return df


def test_cvd_is_cumulative_taker_delta():
    df = _flow_df([6, 2, 5], [10, 10, 10])  # delta: +2, -6, 0
    assert list(cvd(df)) == [2, -4, -4]


def test_absorption_detects_cvd_higher_low_and_buyer_reclaim():
    # idx 3 = swing low (CVD ~ -12), sapuan di idx 8..9 dengan CVD low lebih tinggi
    buys = [5, 5, 3, 2, 4, 6, 6, 5, 4, 8, 9, 9]
    vols = [10] * 12
    df = _flow_df(buys, vols)
    sw = Sweep(level=1, sweep_low=0.9, sweep_idx=8, reclaim_idx=11, depth_pct=0.1,
               lower_wick_ratio=0.5, swing_idx=3)
    a = absorption(df, sw)
    assert a.cvd_higher_low
    assert a.reclaim_delta_pos
    assert a.buy_ratio_3 == pytest.approx(26 / 30)  # buys 8+9+9 dari volume 30


def test_absorption_no_higher_low_when_sellers_dominate_sweep():
    buys = [5, 5, 5, 5, 5, 5, 5, 5, 1, 1, 1, 6]
    df = _flow_df(buys, [10] * 12)
    sw = Sweep(1, 0.9, 8, 11, 0.1, 0.5, swing_idx=3)
    assert not absorption(df, sw).cvd_higher_low


# ------------------------- positioning ------------------------------------
def test_positioning_squeeze_setup_scores_high():
    p = Positioning(funding_rate=-0.0002, retail_long_pct=42, top_long_pct=55, oi_change_pct=-1.2)
    pts, why = score_positioning(p)
    assert pts == 35 and len(why) == 4


def test_positioning_crowded_longs_scores_zero():
    p = Positioning(funding_rate=0.0005, retail_long_pct=70, top_long_pct=60, oi_change_pct=2.0)
    assert score_positioning(p)[0] == 0


# ---------------------------- evaluate ------------------------------------
def test_evaluate_rejects_flat_market_with_reason():
    n = 120
    df = pd.DataFrame({
        "open": [100.0] * n, "high": [100.5] * n, "low": [99.5] * n, "close": [100.0] * n,
        "volume": [10.0] * n, "taker_buy_base": [5.0] * n,
        "time": pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC"),
    })
    sig, why = evaluate("TESTUSDT", df, df, df)
    assert sig is None and "sweep" in why
