"""Live flygtrafik — adsb.lol / adsb.one, keyless. Idé lånad från GeoSentinel.

GeoSentinel (github.com/h9zdev/GeoSentinel) pekade ut ADS-B-community-API:erna
som en öppen, nyckelfri global källa. Deras primära värd (adsb.one) är
Cloudflare-skyddad från vissa nät, så vi provar adsb.lol först och faller
tillbaka — samma svarsformat (readsb/tar1090-schemat).

VARFÖR FLYG I ETT SJÖFARTSPROJEKT: flygfrakt är SUBSTITUTIONSKANALEN när
sjöfrakten stryps. När ett sund stängs flyttar högvärdesgods (elektronik,
läkemedel, reservdelar) från containerfartyg till lastflyg, och flygfraktrater
spikar. Fraktflyg nära ett stört sund är därför en oberoende bekräftelse på
att störningen biter — och en ledande indikator för flygfraktpriser.

Licens: adsb.lol-data är ODbL 1.0 (community-matade mottagare) — attribution
krävs och finns i UI:t.
"""

from __future__ import annotations

import threading
import time

from ..cache import CACHE
from .base import DataSource

# adsb.lol är en GRATIS community-tjänst som drivs av frivilliga mottagare.
# Att skjuta iväg 11 anrop i rad ger strypning (403/429) och slår ut lagret —
# uppmätt i drift. En global takthållare håller oss under ~1 anrop/sekund,
# vilket är den takt deras dokumentation ber om.
_MIN_INTERVAL_S = 1.1
_pace_lock = threading.Lock()
_last_call = [0.0]


def _pace() -> None:
    with _pace_lock:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()

# Tre oberoende community-nätverk. Olika mottagarpopulationer → mergen ger
# fler unika flygplan än någon ensam källa. adsb.fi verifierad 2026-09-07;
# airplanes.live svarar 403 för oss och är utelämnad.
ADSB_HOSTS = ("https://api.adsb.lol", "https://opendata.adsb.fi",
              "https://api.adsb.one")
POINT_PATH = "/v2/point/{lat}/{lon}/{radius}"
# adsb.fi exponerar samma data under readsb-sökvägen
POINT_PATH_FI = "/api/v2/lat/{lat}/lon/{lon}/dist/{radius}"
MIL_PATH = "/v2/mil"          # färdigt militärflygs-endpoint (adsb.lol)
DEFAULT_RADIUS_NM = 250

# Rena fraktflygplanstyper (ICAO-typkoder). Passagerarplan med samma skrov
# filtreras bort av operatörslistan nedan när typen är tvetydig.
FREIGHTER_TYPES = {
    "B77L", "B77F", "B744", "B748", "B762", "B763", "B764", "MD11",
    "A332", "A333", "A30B", "A306", "A310", "B733", "B734", "B735",
    "B752", "B753", "AT72", "AT43", "C130", "IL76", "AN12", "AN24",
    "B462", "B463", "SF34", "CVLT", "DC10", "B722",
}
# Callsign-prefix för de stora fraktoperatörerna (ICAO-koder).
CARGO_CALLSIGNS = (
    "FDX",   # FedEx
    "UPS",   # UPS
    "GTI",   # Atlas Air
    "CLX",   # Cargolux
    "CKS",   # Kalitta Air
    "ABW",   # AirBridgeCargo
    "GEC",   # Lufthansa Cargo
    "BOX",   # AeroLogic
    "CAO",   # Air China Cargo
    "CKK",   # China Cargo
    "SQC",   # Singapore Airlines Cargo
    "QTR",   # Qatar (fraktvarianter)
    "UAE",   # Emirates (SkyCargo)
    "ETH",   # Ethiopian Cargo
    "TAY",   # ASL Airlines
    "NCA",   # Nippon Cargo
    "KAC",   # Kuwait
    "MPH",   # Martinair
    "SVA",   # Saudia Cargo
    "CSN",   # China Southern
    "DHK",   # DHL Air
    "BCS",   # European Air Transport (DHL)
    "AJT",   # Amerijet
    "LTG",   # LATAM Cargo
    "ABX",   # ABX Air
)


def _is_cargo(flight: str, ac_type: str) -> bool:
    """Fraktflyg? Operatörskod väger tyngst; typkod fångar resten.

    Rena fraktoperatörer identifieras på callsign-prefix — det är den enda
    signal som är entydig, eftersom samma skrovtyp (B763, A332) flyger både
    passagerare och frakt.
    """
    cs = (flight or "").strip().upper()
    if cs[:3] in CARGO_CALLSIGNS:
        return True
    return (ac_type or "").upper() in FREIGHTER_TYPES and cs[:3] not in ("SAS", "BAW", "AFR", "DLH", "KLM", "UAL", "AAL", "DAL")


class AdsbSource(DataSource):
    name = "adsb"
    description = "adsb.lol/adsb.one — live flygtrafik (ADS-B), keyless"

    def _fetch_point(self, lat: float, lon: float, radius: int) -> list[dict]:
        """Alla värdars träffar för en punkt, dedupade på hex.

        Vi frågar ALLA nätverk i stället för att stanna vid första svar:
        mottagarpopulationerna överlappar bara delvis, så unionen ger
        märkbart fler unika flygplan."""
        by_hex: dict[str, dict] = {}
        last_exc: Exception | None = None
        for host in ADSB_HOSTS:
            tmpl = POINT_PATH_FI if "adsb.fi" in host else POINT_PATH
            path = tmpl.format(lat=round(lat, 3), lon=round(lon, 3), radius=radius)
            try:
                _pace()
                data = self.http_get_json(host + path)
            except Exception as exc:  # noqa: BLE001 — 403/nätfel → prova nästa värd
                last_exc = exc
                continue
            for ac in data.get("ac") or []:
                h = (ac.get("hex") or "").upper()
                if h and h not in by_hex:
                    by_hex[h] = ac
        if not by_hex and last_exc:
            raise last_exc
        return list(by_hex.values())

    def fetch_around(self, points: list[dict],
                     radius_nm: int = DEFAULT_RADIUS_NM) -> list[dict]:
        """Flygplan inom `radius_nm` av varje punkt {id, lat, lon}, dedupat på hex.

        Varje flygplan bär `near` = id:t för den punkt det hittades vid, så
        aggregeringen per sund/hamn blir gratis."""
        def _fetch():
            by_hex: dict[str, dict] = {}
            for p in points:
                try:
                    rows = self._fetch_point(p["lat"], p["lon"], radius_nm)
                except Exception:  # noqa: BLE001 — en region får inte fälla lagret
                    continue
                for ac in rows:
                    lat, lon = ac.get("lat"), ac.get("lon")
                    hexid = (ac.get("hex") or "").upper()
                    if not hexid or lat is None or lon is None:
                        continue
                    if hexid in by_hex:
                        continue
                    flight = (ac.get("flight") or "").strip()
                    ac_type = ac.get("t") or ""
                    alt = ac.get("alt_baro")
                    by_hex[hexid] = {
                        "hex": hexid,
                        "flight": flight or ac.get("r") or hexid,
                        "type": ac_type,
                        "reg": ac.get("r") or "",
                        "lat": round(float(lat), 5), "lon": round(float(lon), 5),
                        "alt_ft": alt if isinstance(alt, (int, float)) else None,
                        "gs_kn": ac.get("gs"),
                        "track": ac.get("track"),
                        "cargo": _is_cargo(flight, ac_type),
                        "near": p["id"],
                    }
            return list(by_hex.values()) or None

        key = "adsb:around:" + ",".join(sorted(p["id"] for p in points)) + f":{radius_nm}"
        return CACHE.get_or_fetch(key, 60, _fetch) or []

    def safe_fetch_around(self, points: list[dict],
                          radius_nm: int = DEFAULT_RADIUS_NM) -> list[dict]:
        return self._safe("fetch_around",
                          lambda: self.fetch_around(points, radius_nm), [])

    # Globalt svep: ~45 punkter à 250 nm över världens trafikerade luftrum.
    # Mätning 2026-09-07: ~30 % av träffarna finns INTE i OpenSky (andra
    # mottagarnätverk), så svepet är värt sina ~50 s. Körs i bakgrunden.
    GRID = [
        # Europa (tätast)
        (52, 0), (48, 8), (52, 14), (45, 2), (41, 13), (40, -4), (56, 12),
        (55, 24), (46, 25), (38, 27), (60, 18),
        # Nordamerika
        (43, -71), (39, -77), (33, -84), (29, -95), (41, -88), (39, -105),
        (34, -118), (47, -122), (25, -80), (45, -75), (51, -114),
        # Öst- och Sydostasien
        (35, 139), (37, 127), (31, 121), (23, 113), (25, 121), (14, 121),
        (1, 104), (13, 100), (10, 106), (-6, 107),
        # Syd- och Centralasien, Mellanöstern
        (19, 73), (28, 77), (13, 80), (25, 55), (24, 47), (41, 29), (35, 51),
        # Oceanien
        (-34, 151), (-38, 145), (-32, 116), (-37, 175),
        # Sydamerika
        (-23, -46), (-35, -58), (4, -74), (-12, -77), (10, -67),
        # Afrika
        (-26, 28), (30, 31), (6, 3), (-1, 37), (34, -6),
    ]

    def fetch_grid(self, radius_nm: int = 250) -> list[dict]:
        """Globalt rutnätssvep över ADS-B-nätverken. TAR ~50 s — kör i bakgrund."""
        def _fetch():
            by_hex: dict[str, dict] = {}
            for lat, lon in self.GRID:
                try:
                    rows = self._fetch_point(lat, lon, radius_nm)
                except Exception:  # noqa: BLE001 — en punkt får inte fälla svepet
                    continue
                for ac in rows:
                    la, lo = ac.get("lat"), ac.get("lon")
                    h = (ac.get("hex") or "").upper()
                    if not h or la is None or lo is None or h in by_hex:
                        continue
                    flight = (ac.get("flight") or "").strip()
                    ac_type = ac.get("t") or ""
                    alt = ac.get("alt_baro")
                    by_hex[h] = {
                        "hex": h, "flight": flight or ac.get("r") or h,
                        "type": ac_type, "reg": ac.get("r") or "",
                        "lat": round(float(la), 5), "lon": round(float(lo), 5),
                        "alt_ft": alt if isinstance(alt, (int, float)) else None,
                        "gs_kn": ac.get("gs"), "track": ac.get("track"),
                        "cargo": _is_cargo(flight, ac_type),
                        "src": "adsb-grid",
                    }
            return list(by_hex.values()) or None

        return CACHE.get_or_fetch(f"adsbgrid:{radius_nm}", 600, _fetch) or []

    def safe_fetch_grid(self, radius_nm: int = 250) -> list[dict]:
        return self._safe("fetch_grid", lambda: self.fetch_grid(radius_nm), [])

    def fetch_military(self) -> list[dict]:
        """Militära flygplan globalt (adsb.lol:s egen klassning).

        Militär flygaktivitet nära ett sund är en eskaleringsindikator —
        marinspaning (P-8), tankflyg och transportflyg samlas där kriser
        byggs upp. Kompletterar GDACS-larmen med en signal som syns FÖRE
        att något rapporteras."""
        def _fetch():
            last_exc = None
            for host in ADSB_HOSTS:
                try:
                    _pace()
                    data = self.http_get_json(host + MIL_PATH)
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    continue
                out = []
                for ac in data.get("ac") or []:
                    lat, lon = ac.get("lat"), ac.get("lon")
                    if lat is None or lon is None:
                        continue
                    alt = ac.get("alt_baro")
                    out.append({
                        "hex": (ac.get("hex") or "").upper(),
                        "flight": (ac.get("flight") or "").strip() or ac.get("r") or "",
                        "type": ac.get("t") or "",
                        "lat": round(float(lat), 5), "lon": round(float(lon), 5),
                        "alt_ft": alt if isinstance(alt, (int, float)) else None,
                        "gs_kn": ac.get("gs"),
                    })
                return out or None
            if last_exc:
                raise last_exc
            return None

        return CACHE.get_or_fetch("adsb:mil", 180, _fetch) or []

    def safe_fetch_military(self) -> list[dict]:
        return self._safe("fetch_military", self.fetch_military, [])

    def check(self) -> bool:
        return bool(self._fetch_point(51.0, 4.0, 100))
