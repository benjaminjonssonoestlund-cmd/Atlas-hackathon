"""Chokepoint-väder — Open-Meteo (forecast + marine), keyless.

Ett batchanrop per API (komma-separerade koordinater, mönstret från
atlas-earths gas_weather/softs_weather): vind + byar ur forecast-API:t,
signifikant våghöjd ur marine-API:t. Nu-värde + max kommande 48 h — det är
prognosdelen som gör att förseningsrisken kan flaggas INNAN kön syns i AIS.

Trösklar för sjöfartspåverkan sätts i engine/delays.py, inte här — källan
levererar bara mätvärden.
"""

from __future__ import annotations

from ..cache import CACHE
from ..config import CONFIG
from .base import DataSource

FORECAST_API = "https://api.open-meteo.com/v1/forecast"
MARINE_API = "https://marine-api.open-meteo.com/v1/marine"
HOURS_AHEAD = 48


class ChokepointWeatherSource(DataSource):
    name = "weather"
    description = "Open-Meteo — vind/byar/våghöjd per chokepoint (nu + 48 h)"

    def fetch(self, points: list[dict]) -> dict[str, dict]:
        """{cp_id: {wind_ms, gust_ms, wave_m, wind_max48_ms, gust_max48_ms,
                    wave_max48_m}} — värden kan saknas (None) per fält."""
        ids = [p["id"] for p in points]
        lats = ",".join(f"{p['lat']:.3f}" for p in points)
        lons = ",".join(f"{p['lon']:.3f}" for p in points)

        def _fetch():
            out: dict[str, dict] = {i: {} for i in ids}

            wind = self.http_get_json(FORECAST_API, {
                "latitude": lats, "longitude": lons,
                "hourly": "wind_speed_10m,wind_gusts_10m",
                "forecast_days": 3, "windspeed_unit": "ms", "timezone": "UTC"})
            rows = wind if isinstance(wind, list) else [wind]
            for cp_id, row in zip(ids, rows):
                h = (row or {}).get("hourly") or {}
                ws = [v for v in (h.get("wind_speed_10m") or [])[:HOURS_AHEAD]
                      if v is not None]
                gs = [v for v in (h.get("wind_gusts_10m") or [])[:HOURS_AHEAD]
                      if v is not None]
                if ws:
                    out[cp_id]["wind_ms"] = round(ws[0], 1)
                    out[cp_id]["wind_max48_ms"] = round(max(ws), 1)
                if gs:
                    out[cp_id]["gust_ms"] = round(gs[0], 1)
                    out[cp_id]["gust_max48_ms"] = round(max(gs), 1)

            # Marine-API:t saknar data för inlandsnära punkter (Suez ligger i
            # kanalen) — fel för EN punkt får inte fälla batchen, därför safe.
            try:
                waves = self.http_get_json(MARINE_API, {
                    "latitude": lats, "longitude": lons,
                    "hourly": "wave_height", "forecast_days": 3,
                    "timezone": "UTC"})
                wrows = waves if isinstance(waves, list) else [waves]
                for cp_id, row in zip(ids, wrows):
                    h = (row or {}).get("hourly") or {}
                    wv = [v for v in (h.get("wave_height") or [])[:HOURS_AHEAD]
                          if v is not None]
                    if wv:
                        out[cp_id]["wave_m"] = round(wv[0], 1)
                        out[cp_id]["wave_max48_m"] = round(max(wv), 1)
            except Exception:  # noqa: BLE001 — vågdata är berikning, inte krav
                pass

            return out if any(out.values()) else None

        return CACHE.get_or_fetch("weather:chokepoints", CONFIG.ttl_medium,
                                  _fetch) or {i: {} for i in ids}

    def safe_fetch(self, points: list[dict]) -> dict[str, dict]:
        return self._safe("fetch", lambda: self.fetch(points), {})

    def check(self) -> bool:
        return bool(self.fetch([{"id": "test", "lat": 57.0, "lon": 11.0}]))
