"""GDACS — EU/FN:s globala katastrofvarningssystem. Ingen nyckel.

Aktiva varningar för cykloner (TC), översvämningar (FL), jordbävningar (EQ),
torka (DR), skogsbränder (WF) och vulkaner (VO) med alertnivå
GREEN/ORANGE/RED. Direkt ekonomirelevant: en RED-cyklon mot en exporthamn
är en leveranskedjesignal innan den syns i priser.
"""

from __future__ import annotations

from ..cache import CACHE
from ..config import CONFIG
from .base import DataSource

# MAP-endpointen svarar HTTP 400 sedan en tid och slog ut hela lagret tyst —
# cachens circuit breaker öppnade efter tio fel i rad och lagret returnerade
# tom lista i stället för att felas synligt. EVENTS4APP levererar samma
# GeoJSON-form (Point-geometri, samma properties) och är den adress GDACS egen
# app använder.
GDACS_API = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS4APP"

EVENT_TYPES_SV = {"TC": "Tropisk cyklon", "FL": "Översvämning", "EQ": "Jordbävning",
                  "DR": "Torka", "WF": "Skogsbrand", "VO": "Vulkan", "TS": "Tsunami"}


class GdacsSource(DataSource):
    name = "gdacs"
    description = "GDACS — katastrofvarningar med alertnivå (EU/FN)"

    def fetch_alerts(self) -> list[dict]:
        def _fetch():
            data = self.http_get_json(GDACS_API,
                                      headers={"User-Agent": "Mozilla/5.0"})
            alerts = []
            for f in data.get("features", []):
                p = f.get("properties") or {}
                geom = f.get("geometry") or {}
                coords = geom.get("coordinates") or []
                if geom.get("type") != "Point" or len(coords) < 2:
                    continue
                level = (p.get("alertlevel") or "").upper()
                alerts.append({
                    "name": p.get("name") or p.get("eventname") or "",
                    "kind": EVENT_TYPES_SV.get(p.get("eventtype"), p.get("eventtype", "")),
                    # Rakoden (FL/DR/WF/TC/EQ) MÅSTE följa med. `kind` är en
                    # svensk etikett för människor; att låta nedströms gissa
                    # händelsetyp ur den strängen är precis vad `from_gdacs`
                    # gjorde, med engelska sökord som aldrig kunde matcha.
                    "event_code": p.get("eventtype") or "",
                    "episode_id": p.get("episodeid"), "event_id": p.get("eventid"),
                    "severity_text": ((p.get("severitydata") or {}).get("severitytext")
                                      if isinstance(p.get("severitydata"), dict) else None),
                    "from_date": p.get("fromdate"), "to_date": p.get("todate"),
                    "alertlevel": level,
                    "country": p.get("country", ""),
                    "description": (p.get("description") or "")[:300],
                    "url": (p.get("url") or {}).get("report", "")
                           if isinstance(p.get("url"), dict) else (p.get("url") or ""),
                    # Geometri-URL:en ger händelsens FAKTISKA utbredning — för
                    # en cyklon vindsvep per styrkeklass + prognosbana, för en
                    # torka den drabbade ytan. En prick kan inte berätta det.
                    "geometry_url": (p.get("url") or {}).get("geometry", "")
                                    if isinstance(p.get("url"), dict) else "",
                    "lat": coords[1], "lon": coords[0],
                })
            # RED först, sedan ORANGE — GREEN sist (minst akuta)
            order = {"RED": 0, "ORANGE": 1, "GREEN": 2}
            alerts.sort(key=lambda a: order.get(a["alertlevel"], 3))
            return alerts or None

        return CACHE.get_or_fetch("gdacs:alerts", CONFIG.ttl_medium, _fetch) or []

    def safe_fetch_alerts(self) -> list[dict]:
        return self._safe("fetch_alerts", self.fetch_alerts, [])

    def fetch_geometry(self, geometry_url: str) -> dict:
        """FeatureCollection för EN händelse (vindsvep, spår, drabbad yta).

        Ett anrop per händelse — anropas därför bara för ORANGE/RED-larm, inte
        för alla hundra. En aktiv cyklon ger ~90 features; att hämta det för 60
        gröna skogsbränder vore hundra anrop för geometri ingen ser.
        """
        if not geometry_url or not str(geometry_url).startswith("https://www.gdacs.org/"):
            return {}

        def _fetch():
            data = self.http_get_json(str(geometry_url),
                                      headers={"User-Agent": "Mozilla/5.0"})
            return data if isinstance(data, dict) and data.get("features") else None

        import hashlib as _h
        key = "gdacs:geom:" + _h.sha1(str(geometry_url).encode()).hexdigest()[:12]
        return CACHE.get_or_fetch(key, CONFIG.ttl_slow, _fetch) or {}

    def safe_fetch_geometry(self, geometry_url: str) -> dict:
        return self._safe("fetch_geometry",
                          lambda: self.fetch_geometry(geometry_url), {})

    def check(self) -> bool:
        return bool(self.fetch_alerts())
