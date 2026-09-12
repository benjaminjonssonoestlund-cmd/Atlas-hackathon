"""Digitraffic (Fintraffic) — finsk nationell AIS, helt öppen. Ingen nyckel.

Mönster från atlas-earth: finska trafikledningsverkets egna basstationer
täcker Östersjön/Finska viken tätare än frivilligmottagarna. Fartygen mergas
med AISStream-snapshotet per MMSI (AISStream vinner — färskare ström);
nettoeffekten är FLER live-fartyg i Norden, gratis och utan nyckel.

Returnerar rader i SAMMA format som AIS-snapshotets items, så mergen är en
dict-union.
"""

from __future__ import annotations

from ..cache import CACHE
from .base import DataSource

DT_LOCATIONS = "https://meri.digitraffic.fi/api/ais/v1/locations"
DT_VESSELS = "https://meri.digitraffic.fi/api/ais/v1/vessels"
# Digitraffic-User-headern ger generösare rate limit (utan: 60 req/min per IP).
# Attribution enligt CC BY 4.0: "Source: Fintraffic / digitraffic.fi".
DT_HEADERS = {"Digitraffic-User": "atlas-chokepoint/0.1"}


def _category(ship_type: int | None) -> tuple[str, str]:
    t = ship_type or 0
    if 80 <= t <= 89:
        return "tanker", "#ff9b3d"
    if 70 <= t <= 79:
        return "lastfartyg", "#2aff9e"
    if 60 <= t <= 69:
        return "passagerare", "#66b6ff"
    if t == 30:
        return "fiske", "#b46bff"
    if 50 <= t <= 59:
        return "special", "#b46bff"
    return "okänt", "#c0c8d0"


class DigitrafficSource(DataSource):
    name = "digitraffic"
    description = "Digitraffic — finsk nationell AIS (Östersjön), öppen"

    def _vessel_meta(self) -> dict[int, dict]:
        def _fetch():
            rows = self.http_get_json(DT_VESSELS, headers=DT_HEADERS)
            return {v["mmsi"]: v for v in rows if v.get("mmsi")} or None

        return CACHE.get_or_fetch("digitraffic:vessels", 3600, _fetch) or {}

    def fetch_items(self) -> list[dict]:
        """Fartyg i AIS-snapshotets item-format (mergbara per MMSI)."""
        def _fetch():
            data = self.http_get_json(DT_LOCATIONS, headers=DT_HEADERS)
            meta = self._vessel_meta()
            items = []
            for f in data.get("features", []):
                props = f.get("properties") or {}
                coords = (f.get("geometry") or {}).get("coordinates") or []
                mmsi = props.get("mmsi")
                if not mmsi or len(coords) < 2:
                    continue
                m = meta.get(mmsi, {})
                cat, css = _category(m.get("shipType"))
                items.append({
                    "mmsi": mmsi,
                    "name": (m.get("name") or "").strip(),
                    "lat": round(coords[1], 5), "lon": round(coords[0], 5),
                    "sog": props.get("sog"), "cog": props.get("cog"),
                    "nav": props.get("navStat"),
                    "dest": (m.get("destination") or "").strip(),
                    "category": cat, "color": css,
                    "draught_m": round(m["draught"] / 10, 1) if m.get("draught") else None,
                    "loa_m": None, "beam_m": None,
                    "src": "digitraffic",
                })
            return items or None

        return CACHE.get_or_fetch("digitraffic:items", 60, _fetch) or []

    def safe_fetch_items(self) -> list[dict]:
        return self._safe("fetch_items", self.fetch_items, [])

    def check(self) -> bool:
        return bool(self.fetch_items())
