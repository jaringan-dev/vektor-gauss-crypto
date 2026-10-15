"""Klien MCP tipis (streamable HTTP, JSON-RPC) untuk binance-future-endpoint.

Dipakai hanya untuk data posisi/'bandar' pada kandidat yang sudah lolos filter
harga. Kegagalan tidak boleh mematikan bot: panggil lewat `safe_call`, yang
mengembalikan None dan menandai lapisan itu 'tidak tersedia'.
"""
from __future__ import annotations

import json
import logging

import httpx

from .config import SETTINGS

log = logging.getLogger(__name__)


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 30.0):
        self._url = url or SETTINGS.mcp_url
        token = SETTINGS.mcp_token if token is None else token
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        self._http = httpx.Client(headers=headers, timeout=timeout)
        self._id = 0

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        r = self._http.post(self._url, json=body)
        r.raise_for_status()
        msg = self._parse(r.text)
        if "error" in msg:
            raise MCPError(str(msg["error"]))
        return msg["result"]

    @staticmethod
    def _parse(text: str) -> dict:
        """Respons bisa JSON biasa atau SSE ('event: message\\ndata: {...}')."""
        text = text.strip()
        if text.startswith("{"):
            return json.loads(text)
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise MCPError(f"respons MCP tidak terbaca: {text[:200]}")

    def call(self, tool: str, **arguments):
        """Panggil satu tool, kembalikan payload (JSON jika bisa, selain itu teks mentah)."""
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            raise MCPError(f"{tool}: {result.get('content')}")
        # Tool repo ini mengirim data terstruktur di structuredContent; teks markdown
        # hanya untuk dibaca manusia, jadi dipakai sebagai cadangan saja.
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def safe_call(self, tool: str, **arguments):
        """Seperti `call`, tapi mengembalikan None saat gagal (lapisan jadi 'tidak tersedia')."""
        try:
            return self.call(tool, **arguments)
        except (httpx.HTTPError, MCPError, ValueError) as e:
            log.warning("MCP %s gagal: %s", tool, e)
            return None

    def close(self) -> None:
        self._http.close()
