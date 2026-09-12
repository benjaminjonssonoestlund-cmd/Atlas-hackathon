"""Global Fishing Watch Events API — loitering + omlastningar. Gratis token.

GFW kör riktiga algoritmer på satellit-AIS: ett LOITERING-event är ett fartyg
som drivit långsamt på öppet hav under längre tid (deras tröskel, inte vår
gissning), ett ENCOUNTER är två fartyg sida vid sida till havs (omlastning —
klassiskt skuggflotte-beteende för sanktionerad olja). Det är precis de
händelser som bär råvarusignal: loitrande tankers = flytande lager;
omlastningar nära chokepoints = sanktionsflöden.

Token: gratis på globalfishingwatch.org/our-apis (icke-kommersiellt bruk),
sätt GFW_API_TOKEN i .env. Utan token är källan tyst avstängd.

API-form (v3): GET gateway.api.globalfishingwatch.org/v3/events med
datasets[0]=public-global-loitering-events:latest — parametersyntaxen har
varierat mellan versioner, så vi provar båda varianterna innan vi ger upp.
"""

from __future__ import annotations

import datetime as _dt
import os

from ..cache import CACHE
from .base import DataSource

API = "https://gateway.api.globalfishingwatch.org/v3/events"

DATASETS = {
    "loitering": "public-global-loitering-events:latest",
    "encounters": "public-global-encounters-events:latest",
}


class GfwSource(DataSource):
    name = "gfw"
    description = "Global Fishing Watch — loitering/omlastningar (satellit-AIS)"

    def __init__(self) -> None:
        self.token = os.environ.get("GFW_API_TOKEN", "").strip()

    @property
    def available(self) -> bool:
        return bool(self.token)

    def _events_raw(self, dataset: str, days: int) -> list[dict]:
        end = _dt.date.today()
        start = end - _dt.timedelta(days=days)
        headers = {"Authorization": f"Bearer {self.token}"}
        base = {"start-date": start.isoformat(), "end-date": end.isoformat(),
                "limit": "500", "offset": "0"}
        last_exc: Exception | None = None
        # Parametervarianter: datasets[0]= är v3:s dokumenterade form; äldre
        # gateway-versioner tog datasets= (kommaseparerat). Prova i ordning.
        for key in ("datasets[0]", "datasets"):
            try:
                data = self.http_get_json(API, {**base, key: dataset},
                                          headers=headers)
            except Exception as exc:  # noqa: BLE001 — 400/422 → prova nästa form
                last_exc = exc
                continue
            entries = data.get("entries")
            if isinstance(entries, list):
                return entries
        if last_exc:
            raise last_exc
        return []

    def fetch_events(self, kind: str = "loitering", days: int = 7) -> list[dict]:
        """Normaliserade händelser: [{lat, lon, kind, start, end, hours,
        vessel_name, mmsi, vessel_type, flag}]."""
        dataset = DATASETS.get(kind)
        if not dataset or not self.available:
            return []

        def _fetch():
            out = []
            for e in self._events_raw(dataset, days):
                pos = e.get("position") or {}
                lat, lon = pos.get("lat"), pos.get("lon")
                if lat is None or lon is None:
                    continue
                v = e.get("vessel") or {}
                loit = e.get("loitering") or {}
                hours = loit.get("totalTimeHours")
                out.append({
                    "lat": round(float(lat), 4), "lon": round(float(lon), 4),
                    "kind": kind,
                    "start": (e.get("start") or "")[:16],
                    "end": (e.get("end") or "")[:16],
                    "hours": round(float(hours), 1) if hours else None,
                    "vessel_name": (v.get("name") or "").strip(),
                    "mmsi": v.get("ssvid") or "",
                    "vessel_type": (v.get("type") or "").lower(),
                    "flag": v.get("flag") or "",
                })
            return out or None

        return CACHE.get_or_fetch(f"gfw:{kind}:{days}", 3600, _fetch) or []

    def safe_fetch_events(self, kind: str = "loitering", days: int = 7) -> list[dict]:
        return self._safe("fetch_events", lambda: self.fetch_events(kind, days), [])

    def check(self) -> bool:
        return self.available and bool(self.fetch_events("loitering", 3))
