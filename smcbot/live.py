"""Scanner live: candle 15m tertutup → evaluate → log / Telegram / jurnal paper.

  python -m smcbot.live once                 # satu putaran universe
  python -m smcbot.live run                  # loop tiap batas 15 menit
  python -m smcbot.live once --no-mcp        # harga saja (degraded)
  python -m smcbot.live once --no-telegram
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date
from pathlib import Path

import pandas as pd

from .binance_rest import BinanceREST
from .config import SETTINGS, Settings
from .mcp_client import MCPClient
from .paper import append_signal
from .positioning import fetch_positioning
from .positioning_db import positioning_at
from .recorder import DB_PATH, connect as connect_db
from .scorer import Signal, evaluate
from .telegram_notify import notify_signal

log = logging.getLogger("live")


def _wait_next_close(period_s: int = 15 * 60, cushion_s: int = 5) -> None:
    now = time.time()
    nxt = (now // period_s + 1) * period_s + cushion_s
    time.sleep(max(1.0, nxt - now))


class DayGate:
    """Cap harian + dedup simbol+close_time dalam sesi."""

    def __init__(self, max_per_day: int):
        self.max_per_day = max_per_day
        self.day: date | None = None
        self.count = 0
        self.seen: set[tuple[str, int]] = set()

    def reset_if_new_day(self, d: date) -> None:
        if self.day != d:
            self.day = d
            self.count = 0
            self.seen.clear()

    def accept(self, symbol: str, close_ms: int, when: date) -> bool:
        self.reset_if_new_day(when)
        key = (symbol, close_ms)
        if key in self.seen:
            return False
        if self.count >= self.max_per_day:
            return False
        self.seen.add(key)
        self.count += 1
        return True


def _fetch_hybrid(mcp: MCPClient | None, db_path: Path):
    """Utama MCP; fallback DB lokal bila MCP gagal/None."""
    db = connect_db(db_path) if db_path.exists() else None

    def fetch(symbol: str):
        if mcp is not None:
            p = fetch_positioning(mcp, symbol)
            if p is not None:
                return p
        if db is not None:
            return positioning_at(db, symbol, int(time.time() * 1000))
        return None

    fetch.close = (lambda: db.close() if db is not None else None)  # type: ignore[attr-defined]
    return fetch


def scan_once(
    rest: BinanceREST,
    s: Settings = SETTINGS,
    *,
    use_mcp: bool = True,
    use_telegram: bool = True,
    use_paper: bool = True,
    gate: DayGate | None = None,
) -> list[Signal]:
    gate = gate or DayGate(s.max_signals_per_day)
    symbols = rest.top_symbols(s.top_n_pairs, s.min_quote_volume_usd)
    mcp = MCPClient() if use_mcp else None
    fetch = _fetch_hybrid(mcp, DB_PATH) if use_mcp else None
    found: list[Signal] = []
    try:
        for sym in symbols:
            try:
                ltf = rest.closed_klines(sym, s.ltf, limit=s.scan_window + 5)
                h1 = rest.closed_klines(sym, "1h", limit=s.scan_window)
                h4 = rest.closed_klines(sym, "4h", limit=s.scan_window)
            except RuntimeError as e:
                log.warning("%s unduh gagal: %s", sym, e)
                continue
            if len(ltf) < s.scan_window:
                continue
            w = ltf.iloc[-s.scan_window :].reset_index(drop=True)
            close_ms = int(w["close_time"].iloc[-1])
            sig, why = evaluate(sym, w, h1, h4, fetch, s)
            if sig is None:
                log.debug("%s: %s", sym, why)
                continue
            when = pd_date(sig.time)
            if not gate.accept(sym, close_ms, when):
                log.info("lewati %s (dedup/cap harian)", sym)
                continue
            found.append(sig)
            log.info("SINYAL %s skor=%.0f entry=%.6g SL=%.6g TP=%.6g%s",
                     sym, sig.score, sig.plan.entry, sig.plan.sl, sig.plan.tp,
                     " degraded" if sig.degraded else "")
            if use_paper:
                append_signal(sig)
            if use_telegram:
                notify_signal(sig)
    finally:
        if mcp is not None:
            mcp.close()
        if fetch is not None and hasattr(fetch, "close"):
            fetch.close()
    return found


def pd_date(ts) -> date:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.date()


def run_forever(s: Settings = SETTINGS, **kwargs) -> None:
    rest = BinanceREST()
    gate = DayGate(s.max_signals_per_day)
    try:
        while True:
            _wait_next_close()
            try:
                n = len(scan_once(rest, s, gate=gate, **kwargs))
                log.info("putaran selesai: %d sinyal", n)
            except Exception:  # noqa: BLE001
                log.exception("putaran gagal")
    finally:
        rest.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Scanner live sinyal Long SMC")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("once", "run"):
        p = sub.add_parser(name)
        p.add_argument("--no-mcp", action="store_true")
        p.add_argument("--no-telegram", action="store_true")
        p.add_argument("--no-paper", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    kw = dict(use_mcp=not a.no_mcp, use_telegram=not a.no_telegram, use_paper=not a.no_paper)
    if a.cmd == "once":
        rest = BinanceREST()
        try:
            sigs = scan_once(rest, **kw)
            print(f"{len(sigs)} sinyal")
            for sig in sigs:
                print(f"  {sig.symbol} skor={sig.score:.0f} entry={sig.plan.entry} SL={sig.plan.sl} TP={sig.plan.tp}")
        finally:
            rest.close()
    else:
        run_forever(**kw)


if __name__ == "__main__":
    main()
