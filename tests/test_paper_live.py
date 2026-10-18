"""Tes jurnal paper, format Telegram, dan DayGate (tanpa jaringan)."""
from datetime import date

import pandas as pd

from smcbot.config import SETTINGS
from smcbot.live import DayGate
from smcbot.paper import append_signal, summary
from smcbot.risk import TradePlan
from smcbot.scorer import Signal
from smcbot.smc import Sweep
from smcbot.telegram_notify import format_signal


def _sig(symbol="BTCUSDT", score=55.0):
    plan = TradePlan(
        entry=100, sl=97.9, tp=106, risk_pct=2.1, gross_rr=2.8, net_rr=2.5,
        qty=0.05, notional=5, leverage=0.01, risk_usd=5.0,
    )
    sw = Sweep(level=99, sweep_low=98, sweep_idx=10, reclaim_idx=11, depth_pct=1.0,
               lower_wick_ratio=0.5, swing_idx=5)
    return Signal(
        symbol=symbol, score=score, breakdown={"sweep": 20}, reasons=["tes"],
        plan=plan, sweep=sw, zone=None, positioning=None, degraded=True,
        time=pd.Timestamp("2026-01-01 12:00", tz="UTC"),
    )


def test_frozen_threshold_is_single_source():
    from smcbot.calibrate import FROZEN_THRESHOLD
    assert FROZEN_THRESHOLD == SETTINGS.score_threshold_degraded == 37.0
    assert SETTINGS.score_threshold == 50.0


def test_format_signal_contains_symbol_and_levels():
    text = format_signal(_sig())
    assert "BTCUSDT" in text
    assert "100" in text and "97.9" in text and "106" in text
    assert "[degraded]" in text


def test_day_gate_dedup_and_cap():
    g = DayGate(max_per_day=2)
    d = date(2026, 1, 1)
    assert g.accept("A", 1, d)
    assert not g.accept("A", 1, d)  # dedup
    assert g.accept("B", 2, d)
    assert not g.accept("C", 3, d)  # cap
    assert g.accept("C", 3, date(2026, 1, 2))  # hari baru


def test_append_signal_writes_journal(tmp_path, monkeypatch):
    path = tmp_path / "paper_journal.csv"
    monkeypatch.setattr("smcbot.paper.JOURNAL", path)
    monkeypatch.setattr("smcbot.paper.DATA_DIR", tmp_path)
    append_signal(_sig())
    append_signal(_sig())  # dedup waktu+simbol
    assert path.exists()
    assert summary().startswith("total 1")
