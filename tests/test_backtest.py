"""Tes simulasi trade, metrik, aturan portofolio, dan pra-filter backtester."""
import numpy as np
import pandas as pd
import pytest

from smcbot.backtest import (
    Trade, apply_portfolio_rules, candidate_mask, metrics, simulate_trade,
)
from smcbot.config import Settings
from smcbot.risk import plan_long
from smcbot.smc import find_long_sweep

S = Settings(slippage_pct=0.0, max_hold_candles=10)  # slippage 0 agar angka mudah diverifikasi


def candles(rows):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["time"] = pd.date_range("2026-01-01", periods=len(df), freq="15min", tz="UTC")
    return df


@pytest.fixture
def plan():
    p, _ = plan_long(entry=100, sweep_low=98, atr_value=1, swing_high_above=106, s=S)
    return p  # SL 97.9, TP 106


def test_tp_hit_gives_positive_net_r(plan):
    l = candles([(100, 100, 100, 100), (100, 103, 99, 102), (102, 107, 101, 106)])
    outcome, j, r = simulate_trade(plan, l, 0, S)
    assert (outcome, j) == ("TP", 2)
    # ~ +2.68R brutto dikurangi fee; harus positif dan di bawah RR kotor
    assert 2.0 < r < plan.gross_rr


def test_sl_hit_loses_about_one_r_including_fees(plan):
    l = candles([(100, 100, 100, 100), (100, 101, 97.0, 97.5)])
    outcome, j, r = simulate_trade(plan, l, 0, S)
    assert outcome == "SL" and j == 1
    # Perencana memperkirakan fee exit di harga entry; simulasi memakai harga exit sebenarnya
    # (sedikit lebih rendah), jadi rugi sedikit di bawah 1R.
    assert r == pytest.approx(-1.0, abs=0.01)
    assert r > -1.0


def test_same_candle_sl_and_tp_counts_as_sl(plan):
    l = candles([(100, 100, 100, 100), (100, 107, 97.0, 100)])
    assert simulate_trade(plan, l, 0, S)[0] == "SL"


def test_timeout_exits_at_close(plan):
    l = candles([(100, 100, 100, 100)] + [(100, 101, 99.5, 100.5)] * 12)
    outcome, j, r = simulate_trade(plan, l, 0, S)
    assert outcome == "TIMEOUT" and j == 10   # max_hold_candles=10
    assert -0.5 < r < 0.5


def test_slippage_makes_results_strictly_worse(plan):
    l = candles([(100, 100, 100, 100), (100, 103, 99, 102), (102, 107, 101, 106)])
    clean = simulate_trade(plan, l, 0, S)[2]
    slipped = simulate_trade(plan, l, 0, Settings(slippage_pct=0.1, max_hold_candles=10))[2]
    assert slipped < clean


def test_metrics_basic_and_drawdown():
    rs = np.array([1.0, -1.0, -1.0, 2.0, -1.0])
    m = metrics(rs)
    assert m["n"] == 5 and m["win_rate"] == pytest.approx(0.4)
    assert m["avg_r"] == pytest.approx(0.0)
    assert m["total_r"] == pytest.approx(0.0)
    assert m["max_dd_r"] == pytest.approx(2.0)          # 1 -> -1 dari puncak +1
    assert m["profit_factor"] == pytest.approx(1.0)
    assert m["ci95"][0] < 0 < m["ci95"][1]              # tidak bisa dibedakan dari nol


def test_metrics_empty():
    assert metrics(np.array([])) == {"n": 0}


def _tr(sym, minute, exit_minute, score=60.0, day=1):
    t0 = pd.Timestamp(f"2026-01-{day:02d}", tz="UTC")
    return Trade(sym, t0 + pd.Timedelta(minutes=minute), t0 + pd.Timedelta(minutes=exit_minute), score, "TP", 1.0, 1), 0


def test_portfolio_one_position_per_symbol():
    items = [_tr("A", 0, 60), _tr("A", 30, 90), _tr("A", 120, 150)]
    taken = apply_portfolio_rules(items, Settings())
    assert [t.time.minute + t.time.hour * 60 for t in taken] == [0, 120]  # yang ke-2 masih berjalan


def test_portfolio_daily_cap_keeps_highest_score_first():
    items = [_tr(f"S{i}", 0, 30, score=50 + i) for i in range(8)]  # 8 sinyal bersamaan
    taken = apply_portfolio_rules(items, Settings(max_signals_per_day=5))
    assert len(taken) == 5
    assert [t.symbol for t in taken] == ["S7", "S6", "S5", "S4", "S3"]


def test_portfolio_daily_cap_resets_next_day():
    items = [_tr("A", 0, 10, day=1), _tr("B", 0, 10, day=2)]
    assert len(apply_portfolio_rules(items, Settings(max_signals_per_day=1))) == 2


def test_prefilter_never_drops_a_real_sweep():
    """Pada candle sapuan sintetis, mask harus True pada candle sinyal."""
    rng = np.random.default_rng(3)
    base = 100 + np.cumsum(rng.normal(0, 0.3, 300))
    rows = [(c, c + 0.4, c - 0.4, c + 0.05) for c in base]
    lvl = min(r[2] for r in rows[-40:-5])  # likuiditas = low terendah baru-baru ini
    rows.append((base[-1], base[-1] + 0.2, lvl - 0.6, lvl - 0.3))   # sapuan
    rows.append((lvl - 0.3, lvl + 1.5, lvl - 0.7, lvl + 1.2))       # reclaim kuat
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["time"] = pd.date_range("2026-01-01", periods=len(df), freq="15min", tz="UTC")
    sw = find_long_sweep(df)
    if sw is not None:  # bila detektor melihat sweep, pra-filter wajib meloloskannya
        assert candidate_mask(df)[-1]
