"""Marknadsdata — Yahoo Finance chart-API, keyless. Slimmad ur atlas-earth.

Två uppgifter: (1) live-citat för chokepointarnas kopplade instrument till
tickern/panelen, (2) daglig prishistorik för event-studien och prognosbanden.
Yahoo strypter icke-browser-User-Agents från datacenter-IP:n → browser-headers
+ två värdar provas (fungerar keyless; Stooq är dött, se atlas-earth-minnen).
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..cache import CACHE
from ..config import CONFIG
from .base import DataSource

YAHOO_HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
YAHOO_CHART_PATH = "/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
    "Origin": "https://finance.yahoo.com",
}


class MarketSource(DataSource):
    name = "markets"
    description = "Yahoo Finance — citat + dagshistorik för kopplade instrument"

    def _chart(self, symbol: str, **params) -> dict:
        last_exc: Exception | None = None
        for host in YAHOO_HOSTS:
            url = host + YAHOO_CHART_PATH.format(symbol=symbol)
            try:
                data = self.http_get_json(url, params, headers=YAHOO_HEADERS)
            except Exception as exc:  # noqa: BLE001 — 429/nätfel → prova nästa värd
                last_exc = exc
                continue
            result = data.get("chart", {}).get("result")
            if result:
                return result[0]
            last_exc = ValueError(f"tomt chartsvar för {symbol}")
        raise last_exc or ValueError(f"chart misslyckades för {symbol}")

    def fetch_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """{symbol: {price, change_pct, date}} — en trasig symbol fäller inte resten."""
        syms = sorted(set(symbols))
        if not syms:
            return {}

        def one(symbol: str) -> dict:
            res = self._chart(symbol, range="5d", interval="1d")
            meta = res.get("meta", {})
            price = meta.get("regularMarketPrice")
            closes = [c for c in (res.get("indicators", {}).get("quote") or [{}])[0]
                      .get("close", []) if c is not None]
            prev = meta.get("chartPreviousClose")
            if prev is None and len(closes) >= 2:
                prev = closes[-2]
            change = round((price - prev) / prev * 100, 2) if price and prev else None
            date = ""
            if res.get("timestamp"):
                date = datetime.fromtimestamp(res["timestamp"][-1],
                                              tz=timezone.utc).strftime("%Y-%m-%d")
            return {"price": round(price, 2) if price is not None else None,
                    "change_pct": change, "date": date}

        def _fetch():
            out = {}
            for s in syms:
                try:
                    out[s] = one(s)
                except Exception:  # noqa: BLE001
                    out[s] = {"price": None, "change_pct": None, "date": ""}
            return out if any(v["price"] is not None for v in out.values()) else None

        return CACHE.get_or_fetch("yahoo:quotes:" + ",".join(syms),
                                  CONFIG.ttl_medium, _fetch) or {}

    def fetch_history(self, symbol: str, start: str,
                      end: str | None = None) -> list[dict]:
        """Dagliga slutkurser [{date, close}] — underlag för event-studien."""
        end = end or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
        p2 = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp()) + 86400

        def _fetch():
            res = self._chart(symbol, period1=p1, period2=p2, interval="1d")
            stamps = res.get("timestamp") or []
            closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
            rows = []
            for ts, close in zip(stamps, closes):
                if close is None:
                    continue
                rows.append({"date": datetime.fromtimestamp(ts, tz=timezone.utc)
                                             .strftime("%Y-%m-%d"),
                             "close": round(close, 4)})
            return rows or None

        return CACHE.get_or_fetch(f"yahoo:hist:{symbol}:{start}:{end}",
                                  CONFIG.ttl_slow, _fetch) or []

    def safe_fetch_quotes(self, symbols: list[str]) -> dict[str, dict]:
        return self._safe("fetch_quotes", lambda: self.fetch_quotes(symbols), {})

    def safe_fetch_history(self, symbol: str, start: str,
                           end: str | None = None) -> list[dict]:
        return self._safe("fetch_history",
                          lambda: self.fetch_history(symbol, start, end), [])

    def check(self) -> bool:
        return bool(self.fetch_quotes(["BZ=F"]))
