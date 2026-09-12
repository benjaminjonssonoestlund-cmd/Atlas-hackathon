"""OpenSky Network — GLOBAL flygtrafik i realtid. Anonym åtkomst, ingen nyckel.

Källan var redan porterad i atlas-earth (sources/flights.py) och är den enda
öppna som ger HELA världens luftrum i ett anrop: ~13 500 flygplan mot ~100
från punktfrågor mot ADS-B-community-nätverken.

Anonyma anrop är hårt rate-limitade (~1 anrop/10 s för hela världen, och
kvoten är dagsbegränsad), så svaret cachas aggressivt och all filtrering görs
lokalt på den hämtade ögonblicksbilden.

Statvektorns fältordning (OpenSky REST v1, positionsindex som konstanter
nedan) är stabil sedan API:ts start men odokumenterat namngiven — därför
namnger vi indexen i stället för att strö magiska siffror i koden.
"""

from __future__ import annotations

from ..cache import CACHE
from .base import DataSource
from .adsb import _is_cargo

API = "https://opensky-network.org/api/states/all"

# Index i OpenSkys statvektor
I_ICAO, I_CALLSIGN, I_COUNTRY = 0, 1, 2
I_LON, I_LAT, I_ALT_M = 5, 6, 7
I_ON_GROUND, I_VELOCITY, I_TRACK = 8, 9, 10

M_TO_FT = 3.28084


class OpenSkySource(DataSource):
    name = "opensky"
    description = "OpenSky Network — global flygtrafik (anonym, nyckelfri)"

    def fetch_global(self, include_ground: bool = False) -> list[dict]:
        """Alla flygplan i luften just nu, i samma form som ADS-B-lagret."""
        def _fetch():
            data = self.http_get_json(API)
            out = []
            for s in data.get("states") or []:
                if len(s) <= I_TRACK:
                    continue
                lat, lon = s[I_LAT], s[I_LON]
                if lat is None or lon is None:
                    continue
                if s[I_ON_GROUND] and not include_ground:
                    continue
                callsign = (s[I_CALLSIGN] or "").strip()
                alt_m = s[I_ALT_M]
                vel = s[I_VELOCITY]
                out.append({
                    "hex": (s[I_ICAO] or "").upper(),
                    "flight": callsign or (s[I_ICAO] or "").upper(),
                    "type": "",                       # OpenSky ger ingen typkod
                    "reg": "",
                    "country": s[I_COUNTRY] or "",
                    "lat": round(float(lat), 5), "lon": round(float(lon), 5),
                    "alt_ft": round(alt_m * M_TO_FT) if alt_m else None,
                    # m/s → knop
                    "gs_kn": round(vel * 1.94384, 1) if vel else None,
                    "track": s[I_TRACK],
                    # utan typkod kan bara operatörskoden avgöra frakt
                    "cargo": _is_cargo(callsign, ""),
                    "src": "opensky",
                })
            return out or None

        # 10 min: anonyma anrop har dygnskvot, och lagret behöver inte vara
        # färskare än så när 13 500 flygplan ändå extrapoleras i UI:t.
        return CACHE.get_or_fetch("opensky:global", 600, _fetch) or []

    def safe_fetch_global(self) -> list[dict]:
        return self._safe("fetch_global", self.fetch_global, [])

    def check(self) -> bool:
        return bool(self.fetch_global())
