"""Tes loader positioning.db dengan anti-lookahead."""
import pytest

from smcbot import recorder as rec
from smcbot.positioning_db import positioning_at
from smcbot.recorder import PERIOD_MS, available_ms

T0 = 1_792_000_500_000


def _seed(db, symbol="AAAUSDT", n=10):
    ts0 = rec.floor15(T0)
    for k in range(n):
        ts = ts0 - k * PERIOD_MS
        oi = 100.0 + k  # lebih lama = lebih tinggi → change negatif bila newest lebih kecil
        db.execute(
            "INSERT INTO snap (symbol, ts, oi, oi_value, g_long, g_ratio, ta_long, ta_ratio, "
            "tp_long, tp_ratio, taker_buy, taker_sell, taker_ratio) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, ts, oi, oi * 20, 0.42, 0.72, 0.55, 1.2, 0.55, 1.2, 60, 50, 1.2),
        )
        db.execute(
            "INSERT INTO mark (symbol, ts, mark_price, index_price, funding_rate, next_funding_ms) "
            "VALUES (?,?,?,?,?,?)",
            (symbol, ts, 10.0, 10.0, -0.0002, ts + PERIOD_MS),
        )
    db.commit()
    return ts0


def test_positioning_at_uses_latest_available_snapshot():
    db = rec.connect(":memory:")
    ts0 = _seed(db)
    # as_of = ts0 → OI/ratio known; funding from mark
    p = positioning_at(db, "AAAUSDT", ts0)
    assert p is not None
    assert p.funding_rate == pytest.approx(-0.0002)
    assert p.retail_long_pct == pytest.approx(42.0)
    assert p.top_long_pct == pytest.approx(55.0)
    assert p.oi_change_pct < 0  # newest oi lebih kecil dari lookback


def test_positioning_at_none_before_any_data():
    db = rec.connect(":memory:")
    _seed(db)
    assert positioning_at(db, "AAAUSDT", 0) is None


def test_available_ms_contract_for_taker():
    ts = rec.floor15(T0)
    assert available_ms(ts, "taker_ratio") == ts + PERIOD_MS
    assert available_ms(ts, "oi") == ts
