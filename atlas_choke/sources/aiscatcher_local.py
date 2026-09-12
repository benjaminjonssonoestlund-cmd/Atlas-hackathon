"""Lokal AIS-catcher-station (jvde-github/AIS-catcher) — VALFRI källa.

AIS-catchers inbyggda webserver exponerar dokumenterat öppna JSON-endpoints
(CORS aktivt per default på JSON/GeoJSON/KML). Kör man en egen station med
RTL-SDR (+ sharing key `-X` mot aiscatcher.org) hörs egna fartyg ~40-70 km
och community-feeden låses upp i stationens UI — reciprocitetsmodellen.

Sätt ATLAS_AISCATCHER_URL (t.ex. http://127.0.0.1:8100) så mergas stationens
fartyg in i live-bilden. Utan variabeln är källan tyst avstängd.

Endpoint: {url}/ships_array.json — kompakt array-format (fältordningen
dokumenterad i AIS-catcher-källan); {url}/ships.json finns som objektform
i äldre versioner — vi provar båda.
"""

from __future__ import annotations

import os

from ..cache import CACHE
from .base import DataSource
from .digitraffic import _category


class AisCatcherLocalSource(DataSource):
    name = "aiscatcher"
    description = "Egen AIS-catcher-station (lokal webserver), valfri"

    def __init__(self) -> None:
        self.base_url = os.environ.get("ATLAS_AISCATCHER_URL", "").strip().rstrip("/")

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    def fetch_items(self) -> list[dict]:
        if not self.available:
            return []

        def _fetch():
            # Schema verifierat mot källkoden (Tracking/Ships.cpp writeJSON):
            # mmsi, lat, lon, speed, cog, heading, shiptype (AIS-typkod),
            # status (NavStatus), draught, shipname, destination, imo,
            # callsign, to_bow/to_stern/to_port/to_starboard (dimensioner →
            # loa/beam, vilket matar tonnage-proxyn).
            data = self.http_get_json(self.base_url + "/api/ships.json")
            rows = data.get("ships") or []
            items = []
            for r in rows:
                mmsi = r.get("mmsi")
                lat, lon = r.get("lat"), r.get("lon")
                if mmsi is None or lat is None or lon is None:
                    continue
                cat, css = _category(r.get("shiptype"))
                loa = (r.get("to_bow") or 0) + (r.get("to_stern") or 0)
                beam = (r.get("to_port") or 0) + (r.get("to_starboard") or 0)
                items.append({
                    "mmsi": mmsi,
                    "name": (r.get("shipname") or "").strip(),
                    "lat": round(float(lat), 5), "lon": round(float(lon), 5),
                    "sog": r.get("speed"), "cog": r.get("cog"),
                    "nav": r.get("status"),
                    "dest": (r.get("destination") or "").strip(),
                    "category": cat, "color": css,
                    "draught_m": r.get("draught") or None,
                    "loa_m": round(loa, 1) if loa else None,
                    "beam_m": round(beam, 1) if beam else None,
                    "src": "aiscatcher",
                })
            return items or None

        return CACHE.get_or_fetch("aiscatcher:items", 30, _fetch) or []

    def safe_fetch_items(self) -> list[dict]:
        return self._safe("fetch_items", self.fetch_items, [])

    def check(self) -> bool:
        return self.available and bool(self.fetch_items())
