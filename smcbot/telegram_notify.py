"""Kirim alert Telegram (Bot API). No-op bila token/chat_id kosong."""
from __future__ import annotations

import logging

import httpx

from .config import SETTINGS
from .scorer import Signal

log = logging.getLogger(__name__)


def format_signal(sig: Signal) -> str:
    p = sig.plan
    deg = " [degraded]" if sig.degraded else ""
    reasons = "\n".join(f"• {r}" for r in sig.reasons[:8])
    return (
        f"LONG {sig.symbol}{deg}\n"
        f"skor {sig.score:.0f}  entry {p.entry:.6g}  SL {p.sl:.6g}  TP {p.tp:.6g}\n"
        f"RR bersih {p.net_rr:.2f}  risiko ${p.risk_usd:.2f}  lev {p.leverage:.1f}x\n"
        f"{reasons}"
    )


def send_message(text: str, token: str | None = None, chat_id: str | None = None) -> bool:
    token = SETTINGS.tg_token if token is None else token
    chat_id = SETTINGS.tg_chat_id if chat_id is None else chat_id
    if not token or not chat_id:
        log.debug("Telegram dilewati: token/chat_id kosong")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=20.0)
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        log.warning("Telegram gagal: %s", e)
        return False


def notify_signal(sig: Signal) -> bool:
    return send_message(format_signal(sig))
