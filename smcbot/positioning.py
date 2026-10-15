"""Data posisi 'bandar vs retail' dari MCP. None bila lapisan ini tidak tersedia."""
from __future__ import annotations

from dataclasses import dataclass

from .mcp_client import MCPClient


@dataclass(frozen=True)
class Positioning:
    funding_rate: float        # per periode funding (fraksi, mis. -0.0001 = -0.01%)
    retail_long_pct: float     # akun global, proksi retail
    top_long_pct: float        # posisi top trader
    oi_change_pct: float       # perubahan OI pada jendela terakhir


def fetch_positioning(mcp: MCPClient, symbol: str) -> Positioning | None:
    """Semua empat panggilan harus berhasil; sebagian data lebih menyesatkan daripada tanpa data."""
    fr = mcp.safe_call("binance_get_funding_rate", symbol=symbol)
    retail = mcp.safe_call("binance_get_long_short_ratio", symbol=symbol, period="15m", limit=2)
    top = mcp.safe_call("binance_get_top_trader_ratio", symbol=symbol, mode="position", period="15m", limit=2)
    oi = mcp.safe_call("binance_get_open_interest_history", symbol=symbol, period="15m", limit=8)
    try:
        return Positioning(
            funding_rate=float(fr["fundingRate"]),
            retail_long_pct=float(retail["longPct"]),
            top_long_pct=float(top["longPct"]),
            oi_change_pct=float(oi["changePct"]),
        )
    except (TypeError, KeyError, ValueError):
        return None
