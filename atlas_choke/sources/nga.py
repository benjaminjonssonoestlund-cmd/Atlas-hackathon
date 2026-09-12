"""NGA MSI — globala navigationsvarningar (GMDSS/NAVAREA). Ingen nyckel.

US National Geospatial-Intelligence Agency publicerar aktiva sjövarningar
öppet. Det är HÄR drivande fartyg rapporteras officiellt: "VESSEL ADRIFT",
"DERELICT VESSEL", "VESSEL DISABLED AND ADRIFT", "VESSEL UNDER TOW" — ofta
timmar innan de syns någon annanstans. En drivande tanker i en farled är
både en kollisions- och en utbudssignal.

Positioner ligger inbäddade i varningstexten ("12-34.5N 045-12.3E" m.fl.
format) — vi parsar första koordinaten med ett tolerant regex och hoppar
över varningar utan läsbar position (hellre färre punkter än fel punkter).
"""

from __future__ import annotations

import re

from ..cache import CACHE
from ..config import CONFIG
from .base import DataSource

API = "https://msi.nga.mil/api/publications/broadcast-warn"

# Nyckelord som indikerar drivande/manöveroförmögna fartyg (versal text i källan)
DRIFT_KEYWORDS = [
    "ADRIFT", "DERELICT", "DISABLED", "DRIFTING", "NOT UNDER COMMAND",
    "UNDER TOW", "TAKEN IN TOW",
]

# "12-34.5N 045-12.3E" / "12-34N 045-12E" — grader-minuter med N/S + E/W
_COORD_RE = re.compile(
    r"(\d{1,2})-(\d{1,2}(?:\.\d+)?)\s*([NS])[\s,]+(\d{1,3})-(\d{1,2}(?:\.\d+)?)\s*([EW])")


def _first_position(text: str) -> tuple[float, float] | None:
    m = _COORD_RE.search(text or "")
    if not m:
        return None
    try:
        lat = float(m.group(1)) + float(m.group(2)) / 60.0
        lon = float(m.group(4)) + float(m.group(5)) / 60.0
    except ValueError:
        return None
    if m.group(3) == "S":
        lat = -lat
    if m.group(6) == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 4), round(lon, 4)


class NgaWarningsSource(DataSource):
    name = "nga"
    description = "NGA MSI — aktiva sjövarningar (drivande fartyg m.m.)"

    def _active(self) -> list[dict]:
        def _fetch():
            data = self.http_get_json(API, {"status": "active", "output": "json"})
            rows = data.get("broadcast-warn")
            return rows if isinstance(rows, list) and rows else None

        return CACHE.get_or_fetch("nga:active", CONFIG.ttl_medium, _fetch) or []

    def fetch_drift_warnings(self) -> list[dict]:
        """Varningar som nämner drivande/manöveroförmögna fartyg, med position."""
        def _build():
            out = []
            for w in self._active():
                text = (w.get("text") or "").upper()
                hits = [k for k in DRIFT_KEYWORDS if k in text]
                if not hits:
                    continue
                pos = _first_position(text)
                if pos is None:
                    continue
                out.append({
                    "lat": pos[0], "lon": pos[1],
                    "keywords": hits,
                    "id": f"{w.get('navArea', '')} {w.get('msgYear', '')}/"
                          f"{w.get('msgNumber', '')}".strip(),
                    "issued": (w.get("issueDate") or "")[:20],
                    # trimmat utdrag räcker för panelen; hela texten är lång
                    "text": (w.get("text") or "")[:280],
                })
            return out or None

        return CACHE.get_or_fetch("nga:drift", CONFIG.ttl_medium, _build) or []

    def safe_fetch_drift_warnings(self) -> list[dict]:
        return self._safe("fetch_drift_warnings", self.fetch_drift_warnings, [])

    def check(self) -> bool:
        return bool(self._active())
