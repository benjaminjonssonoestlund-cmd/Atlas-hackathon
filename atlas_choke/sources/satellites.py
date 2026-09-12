"""Satellitbanor — Celestrak GP-katalog (TLE), keyless. Idé från Flowm/satvis.

satvis (github.com/Flowm/satvis) visar 12 000+ satelliter live på en Cesium-glob
med passageprediktion. Vi lånar konceptet men väljer satelliter som betyder
något för det här systemet i stället för hela katalogen:

  * SAR-satelliter (Sentinel-1, RADARSAT, ICEYE, Capella) — det är DE som
    producerar radarbilderna vårt mörka-fartyg-lager bygger på. Att visa när
    Sentinel-1 nästa gång passerar ett sund kopplar ihop banmekaniken med
    frågan "när kan vi verifiera att sundet är tomt?".
  * Optiska jordobservationssatelliter (Sentinel-2, Landsat).
  * Navigations- och kommunikationssatelliter som sjöfarten beror av.

Celestrak ber om måttfull användning: TLE:er ändras långsamt, så svaret
cachas i 12 timmar och grupperna hämtas med paus emellan.
"""

from __future__ import annotations

import threading
import time

from ..cache import CACHE
from .base import DataSource

GP_URL = "https://celestrak.org/NORAD/elements/gp.php"

# Celestrak-grupper vi hämtar. "resource" = jordobservation (Sentinel/Landsat),
# "science" = forskningssatelliter, "gnss" = navigationssystem.
GROUPS = ("resource", "science", "gnss")

# Satelliter som lyfts fram och får bana utritad. SAR först — de är
# källan till vårt eget radarlager.
HIGHLIGHT = {
    "SENTINEL-1A": {"role": "SAR-radar", "why": "Källan till vårt radarlager — ser fartyg utan AIS"},
    "SENTINEL-1C": {"role": "SAR-radar", "why": "Källan till vårt radarlager — ser fartyg utan AIS"},
    "RADARSAT-2": {"role": "SAR-radar", "why": "Radarövervakning av havsområden"},
    "SENTINEL-2A": {"role": "Optisk", "why": "Optiska bilder av hamnar och kust"},
    "SENTINEL-2B": {"role": "Optisk", "why": "Optiska bilder av hamnar och kust"},
    "SENTINEL-3A": {"role": "Havsövervakning", "why": "Havsyta, vindar, is"},
    "SENTINEL-3B": {"role": "Havsövervakning", "why": "Havsyta, vindar, is"},
    "LANDSAT 8": {"role": "Optisk", "why": "Långtidsserier över kustinfrastruktur"},
    "LANDSAT 9": {"role": "Optisk", "why": "Långtidsserier över kustinfrastruktur"},
}

_fetch_lock = threading.Lock()


class SatelliteSource(DataSource):
    name = "satellites"
    description = "Celestrak GP/TLE — satellitbanor (nyckelfritt)"

    def fetch_tles(self) -> list[dict]:
        """[{name, norad, tle1, tle2, role, why}] för de valda grupperna."""
        def _fetch():
            rows: dict[int, dict] = {}
            with _fetch_lock:
                for i, group in enumerate(GROUPS):
                    if i:
                        time.sleep(2.0)      # Celestrak ber om måttfullhet
                    try:
                        text = self.http_get_text(
                            GP_URL + f"?GROUP={group}&FORMAT=tle")
                    except Exception:  # noqa: BLE001 — en grupp får inte fälla lagret
                        continue
                    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
                    for j in range(0, len(lines) - 2, 3):
                        name = lines[j].strip()
                        l1, l2 = lines[j + 1], lines[j + 2]
                        if not l1.startswith("1 ") or not l2.startswith("2 "):
                            continue
                        try:
                            norad = int(l1[2:7])
                        except ValueError:
                            continue
                        hl = HIGHLIGHT.get(name.upper())
                        rows[norad] = {
                            "name": name, "norad": norad,
                            "tle1": l1, "tle2": l2, "group": group,
                            "role": (hl or {}).get("role", ""),
                            "why": (hl or {}).get("why", ""),
                            "highlight": bool(hl),
                        }
            return list(rows.values()) or None

        # TLE:er ändras långsamt; 12 h är gott och snällt mot Celestrak.
        return CACHE.get_or_fetch("celestrak:tle", 43200, _fetch) or []

    def safe_fetch_tles(self) -> list[dict]:
        return self._safe("fetch_tles", self.fetch_tles, [])

    def check(self) -> bool:
        return bool(self.fetch_tles())
