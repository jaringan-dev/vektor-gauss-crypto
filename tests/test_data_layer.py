"""Smoke test data layer. Butuh internet (endpoint publik, read-only)."""
import time

import pytest

from smcbot.binance_rest import BinanceREST
from smcbot.mcp_client import MCPClient


@pytest.fixture(scope="module")
def rest():
    c = BinanceREST()
    yield c
    c.close()


def test_closed_klines_has_no_running_candle(rest):
    df = rest.closed_klines("BTCUSDT", "15m", 100)
    assert len(df) >= 90
    assert df["close_time"].iloc[-1] < time.time() * 1000
    assert df["time"].is_monotonic_increasing
    assert {"open", "high", "low", "close", "taker_buy_base"} <= set(df.columns)


def test_klines_range_paginates_without_duplicates(rest):
    end = int(time.time() * 1000)
    start = end - 3 * 24 * 3600 * 1000  # 3 hari x 96 = ~288 candle
    df = rest.klines_range("ETHUSDT", "15m", start, end)
    assert 280 <= len(df) <= 300
    assert df["open_time"].is_unique


def test_top_symbols_filters_by_volume(rest):
    syms = rest.top_symbols(20, 50_000_000)
    assert "BTCUSDT" in syms and 1 < len(syms) <= 20
    assert all(s.endswith("USDT") for s in syms)


def test_mcp_returns_structured_dict():
    c = MCPClient()
    out = c.call("binance_get_top_trader_ratio", symbol="BTCUSDT", period="15m", limit=3)
    c.close()
    assert isinstance(out, dict)
    assert {"longPct", "shortPct", "ratio"} <= set(out)


def test_mcp_safe_call_returns_none_on_bad_tool():
    c = MCPClient()
    assert c.safe_call("tool_yang_tidak_ada") is None
    c.close()
