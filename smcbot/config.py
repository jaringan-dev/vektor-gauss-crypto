"""Konfigurasi terpusat. Semua angka yang boleh di-tuning ada di sini."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # --- Akses data ---
    binance_base: str = "https://fapi.binance.com"
    mcp_url: str = field(default_factory=lambda: os.getenv("MCP_URL", "https://binance.jaringan.dev/mcp"))
    mcp_token: str = field(default_factory=lambda: os.getenv("MCP_AUTH_TOKEN", ""))

    # --- Telegram ---
    tg_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    tg_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # --- Universe ---
    top_n_pairs: int = 75
    min_quote_volume_usd: float = 50_000_000  # volume 24 jam minimum

    # --- Risiko ---
    account_usd: float = 500.0
    risk_per_trade_pct: float = 1.0  # => $5
    taker_fee_pct: float = 0.05      # per sisi
    min_net_rr: float = 1.5          # RR bersih setelah fee
    daily_loss_limit_r: float = 3.0
    max_signals_per_day: int = 5

    # --- Skor & filter sinyal ---
    # Ambang beku kalibrasi 2026-10-16 (harga-only). Jangan diubah untuk hold-out.
    score_threshold_degraded: float = 37.0  # = FROZEN_THRESHOLD; skor maks 65 tanpa posisi
    score_threshold: float = 50.0           # dengan posisi: butuh ~13 poin lapisan posisi di atas aturan beku
    prefilter_score: float = 35.0           # skor harga minimum sebelum MCP/DB dipanggil
    min_sweep_atr: float = 0.3              # sapuan lebih dangkal = noise
    max_sweep_atr: float = 2.5              # sapuan lebih dalam = lebih mirip breakdown
    sl_buffer_atr: float = 0.1
    tp_max_r: float = 3.0
    max_leverage: float = 5.0

    # --- Backtest ---
    slippage_pct: float = 0.03        # penalti per eksekusi market (entry & SL)
    max_hold_candles: int = 96        # 24 jam; lewat itu keluar di close
    scan_window: int = 250            # candle 15m yang dilihat per evaluasi

    # --- Timeframe ---
    ltf: str = "15m"
    htf: tuple[str, ...] = ("1h", "4h")

    @property
    def risk_usd(self) -> float:
        return self.account_usd * self.risk_per_trade_pct / 100


SETTINGS = Settings()
