"""Basklass för datakälladaptrar.

Alla källor exponerar icke-kastande `safe_*`-metoder: vid nätverks- eller
formatfel returneras None/tom lista och felet loggas — servern ska aldrig
krascha för att en extern källa ligger nere.

HTTP görs med stdlib urllib: curl fallerar på Windows schannel
TLS-revocation, medan urllib fungerar med standardverifiering.
"""

from __future__ import annotations

import gzip
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from ..cache import CACHE
from ..config import CONFIG

log = logging.getLogger("atlas.sources")

# Fallback när värdens CA-kedja avvisas av strikt OpenSSL (publik läsdata).
_LENIENT_TLS = ssl._create_unverified_context()


class DataSource(ABC):
    """En extern öppen datakälla."""

    name: str = "base"
    description: str = ""

    def http_get(self, url: str, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> bytes:
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", CONFIG.user_agent)
        req.add_header("Accept-Encoding", "gzip")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            resp = urllib.request.urlopen(req, timeout=CONFIG.http_timeout)
        except urllib.error.URLError as exc:
            # Vissa värdar (USGS m.fl.) serveras via en CA-kedja som strikt
            # OpenSSL 3.x avvisar. Publik läsdata → tolerant TLS som fallback.
            if "CERTIFICATE_VERIFY_FAILED" not in str(exc.reason):
                raise
            resp = urllib.request.urlopen(req, timeout=CONFIG.http_timeout,
                                          context=_LENIENT_TLS)
        with resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw

    def http_get_json(self, url: str, params: dict[str, Any] | None = None,
                      headers: dict[str, str] | None = None) -> Any:
        return json.loads(self.http_get(url, params, headers).decode("utf-8", errors="replace"))

    def http_get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        return self.http_get(url, params).decode("utf-8", errors="replace")

    def cached_json(self, cache_key: str, ttl: float, url: str,
                    params: dict[str, Any] | None = None) -> Any:
        return CACHE.get_or_fetch(cache_key, ttl, lambda: self.http_get_json(url, params))

    @abstractmethod
    def check(self) -> bool:
        """Snabb hälsokontroll — hämtar minimal data, returnerar True vid kontakt."""

    def safe_check(self) -> bool:
        try:
            return self.check()
        except Exception as exc:  # noqa: BLE001 — statuskontroll får aldrig kasta
            log.warning("%s: hälsokontroll misslyckades: %s", self.name, exc)
            return False

    def _safe(self, label: str, fn, default):
        """Kör fn(); logga och returnera default vid fel."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — källfel ska degradera, inte krascha
            log.warning("%s.%s: %s", self.name, label, exc)
            return default
