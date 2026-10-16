"""Klien REST publik Binance USDT-M Futures (read-only, tanpa API key)."""
from __future__ import annotations

import time

import httpx
import pandas as pd

from .config import SETTINGS

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "_ignore",
]
_NUMERIC = ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]
_MAX_PER_CALL = 1500


class BinanceREST:
    def __init__(self, base: str | None = None, timeout: float = 30.0):
        self._http = httpx.Client(base_url=base or SETTINGS.binance_base, timeout=timeout)

    def _get(self, path: str, params: dict | None = None, retries: int = 6):
        """GET dengan backoff. 429 = lewat batas bobot per menit: tunggu sesuai Retry-After (atau naik
        bertahap sampai 60 dtk). 418 = IP diblokir sementara: berhenti, jangan menekan lagi."""
        for attempt in range(retries):
            try:
                r = self._http.get(path, params=params)
            except httpx.TransportError:  # timeout/putus sesaat: ulangi dengan jeda, jangan gugurkan seluruh unduhan
                if attempt == retries - 1:
                    raise
                time.sleep(2 * 2 ** attempt)
                continue
            if r.status_code == 418:
                raise RuntimeError(f"Binance memblokir IP sementara (418) di {path}; Retry-After={r.headers.get('retry-after')}")
            if r.status_code == 429:
                time.sleep(min(float(r.headers.get("retry-after", 0)) or 5 * 2 ** attempt, 60))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Binance rate-limited: {path}")

    def top_symbols(self, n: int, min_quote_volume: float) -> list[str]:
        """Pair USDT perpetual teratas berdasarkan volume 24 jam."""
        tickers = self._get("/fapi/v1/ticker/24hr")
        info = self._get("/fapi/v1/exchangeInfo")
        live = {
            s["symbol"] for s in info["symbols"]
            if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
        }
        rows = [(t["symbol"], float(t["quoteVolume"])) for t in tickers if t["symbol"] in live]
        rows = [r for r in rows if r[1] >= min_quote_volume]
        rows.sort(key=lambda r: r[1], reverse=True)
        return [s for s, _ in rows[:n]]

    def klines(self, symbol: str, interval: str, limit: int = 500,
               start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
        """Satu halaman klines. Candle terakhir bisa masih berjalan (belum close)."""
        params: dict = {"symbol": symbol, "interval": interval, "limit": min(limit, _MAX_PER_CALL)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        return self._to_frame(self._get("/fapi/v1/klines", params))

    def klines_range(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """Klines historis lintas halaman, untuk backtest."""
        frames, cursor = [], start_ms
        while cursor < end_ms:
            df = self.klines(symbol, interval, _MAX_PER_CALL, start_ms=cursor, end_ms=end_ms)
            time.sleep(0.5)  # halaman 1500 candle berbobot 10; <= ~120 panggilan/menit = aman di bawah 2400/menit
            if df.empty:
                break
            frames.append(df)
            cursor = int(df["open_time"].iloc[-1]) + 1
            if len(df) < _MAX_PER_CALL:
                break
        if not frames:
            return pd.DataFrame(columns=KLINE_COLS)
        return pd.concat(frames, ignore_index=True).drop_duplicates("open_time")

    def closed_klines(self, symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
        """Klines dengan candle yang belum close dibuang, supaya sinyal tidak repaint."""
        df = self.klines(symbol, interval, limit)
        now_ms = int(time.time() * 1000)
        return df[df["close_time"] < now_ms].reset_index(drop=True)

    @staticmethod
    def _to_frame(raw: list) -> pd.DataFrame:
        df = pd.DataFrame(raw, columns=KLINE_COLS).drop(columns="_ignore")
        df[_NUMERIC] = df[_NUMERIC].astype(float)
        df["open_time"] = df["open_time"].astype("int64")
        df["close_time"] = df["close_time"].astype("int64")
        df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        return df

    def close(self) -> None:
        self._http.close()
