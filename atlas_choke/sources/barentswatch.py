"""BarentsWatch Live AIS — norska EEZ + Svalbard, gratis konto. VALFRI källa.

Samma underliggande data som Kystverkets öppna NMEA-ström (som inte svarade
vid test 2026-09-05) men via ett stabilt JSON-API. Registrera gratis på
developer.barentswatch.no → API-klient → sätt BARENTSWATCH_CLIENT_ID och
BARENTSWATCH_CLIENT_SECRET. Utan variabler är källan tyst avstängd.

/v1/latest/combined ger position + statik mergat per fartyg i ett svar —
perfekt för snapshotmergen. Licens: NLOD 2.0.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

from ..cache import CACHE
from .base import DataSource
from .digitraffic import _category

TOKEN_URL = "https://id.barentswatch.no/connect/token"
LATEST_URL = "https://live.ais.barentswatch.no/v1/latest/combined"


class BarentsWatchSource(DataSource):
    name = "barentswatch"
    description = "BarentsWatch Live AIS — norska EEZ (gratis konto)"

    def __init__(self) -> None:
        self.client_id = os.environ.get("BARENTSWATCH_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("BARENTSWATCH_CLIENT_SECRET", "").strip()
        self._token: str | None = None
        self._token_exp = 0.0

    @property
    def available(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _get_token(self) -> str | None:
        if not self.available:
            return None
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        # Scope: AIS-API:t dokumenterar "ais", men klienter skapade i portalen
        # kan ha scopet "api" — prova båda (BARENTSWATCH_SCOPE styr första valet).
        scopes = [os.environ.get("BARENTSWATCH_SCOPE", "ais").strip(), "api", "ais"]
        last_exc: Exception | None = None
        for scope in dict.fromkeys(scopes):
            body = urllib.parse.urlencode({
                "client_id": self.client_id, "client_secret": self.client_secret,
                "scope": scope, "grant_type": "client_credentials"}).encode()
            req = urllib.request.Request(TOKEN_URL, data=body)
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    d = json.loads(resp.read().decode())
            except Exception as exc:  # noqa: BLE001 — fel scope ger 400
                last_exc = exc
                continue
            self._token = d.get("access_token")
            if self._token:
                self._token_exp = time.time() + float(d.get("expires_in", 3600))
                return self._token
        if last_exc:
            raise last_exc
        return None

    def fetch_items(self) -> list[dict]:
        """Fartyg i AIS-snapshotets item-format. Tom lista utan konto."""
        if not self.available:
            return []

        def _fetch():
            token = self._get_token()
            if not token:
                return None
            rows = self.http_get_json(LATEST_URL,
                                      headers={"Authorization": f"Bearer {token}"})
            items = []
            for r in rows or []:
                lat, lon = r.get("latitude"), r.get("longitude")
                mmsi = r.get("mmsi")
                if mmsi is None or lat is None or lon is None:
                    continue
                cat, css = _category(r.get("shipType"))
                items.append({
                    "mmsi": mmsi,
                    "name": (r.get("name") or "").strip(),
                    "lat": round(lat, 5), "lon": round(lon, 5),
                    "sog": r.get("speedOverGround"),
                    "cog": r.get("courseOverGround"),
                    "nav": r.get("navigationalStatus"),
                    "dest": (r.get("destination") or "").strip(),
                    "category": cat, "color": css,
                    "draught_m": r.get("draught"),
                    "loa_m": None, "beam_m": None,
                    "src": "barentswatch",
                })
            return items or None

        return CACHE.get_or_fetch("barentswatch:items", 60, _fetch) or []

    def safe_fetch_items(self) -> list[dict]:
        return self._safe("fetch_items", self.fetch_items, [])

    def check(self) -> bool:
        return self.available and bool(self.fetch_items())
