"""Banpropagering och passageprediktion (SGP4).

Två uppgifter:

  1. `positions()` — var varje satellit är just nu (lat/lon/höjd), för globen.
  2. `next_passes()` — när nästa SAR-satellit passerar över respektive
     chokepoint. Det är den intressanta kopplingen i det här systemet: vårt
     mörka-fartyg-lager bygger på Sentinel-1-radar, och passagetiden svarar
     på "när kan vi verifiera att sundet faktiskt är tomt?".

Passagen definieras som närmaste passage inom `SWATH_KM` av punkten under
kommande `HORIZON_H` timmar, sökt i steg om `STEP_S`. Sentinel-1:s IW-svep är
~250 km brett, så 300 km är en rimlig "kan avbildas"-radie.

Koordinatomvandlingen är ECI → geodetisk via GMST-rotation. Enkel sfärisk
approximation för latituden räcker: felet är någon tiondels grad, långt under
svepbredden.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

SWATH_KM = 300.0     # inom detta räknas punkten som avbildbar
HORIZON_H = 24       # sök så här långt fram
STEP_S = 60          # sökupplösning
EARTH_R = 6378.137


def _gmst(jd: float) -> float:
    """Greenwich Mean Sidereal Time (radianer) för ett julianskt datum."""
    t = (jd - 2451545.0) / 36525.0
    g = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
         + 0.000387933 * t * t - t * t * t / 38710000.0)
    return math.radians(g % 360.0)


def _eci_to_geodetic(x: float, y: float, z: float, jd: float) -> tuple:
    """ECI (km) → (lat°, lon°, höjd km)."""
    theta = _gmst(jd)
    lon = math.degrees(math.atan2(y, x) - theta)
    lon = (lon + 180.0) % 360.0 - 180.0
    r = math.sqrt(x * x + y * y + z * z)
    lat = math.degrees(math.asin(max(-1.0, min(1.0, z / r))))
    return lat, lon, r - EARTH_R


def _jday(dt: datetime) -> tuple:
    from sgp4.api import jday
    return jday(dt.year, dt.month, dt.day, dt.hour, dt.minute,
                dt.second + dt.microsecond / 1e6)


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def positions(tles: list[dict], when: datetime | None = None) -> list[dict]:
    """Nuvarande position för varje satellit. Trasiga TLE:er hoppas över."""
    from sgp4.api import Satrec
    when = when or datetime.now(timezone.utc)
    jd, fr = _jday(when)
    out = []
    for t in tles:
        try:
            sat = Satrec.twoline2rv(t["tle1"], t["tle2"])
            e, r, _v = sat.sgp4(jd, fr)
        except Exception:  # noqa: BLE001 — enstaka dålig TLE ska inte fälla lagret
            continue
        if e != 0:
            continue
        lat, lon, alt = _eci_to_geodetic(r[0], r[1], r[2], jd + fr)
        out.append({
            "name": t["name"], "norad": t["norad"],
            "lat": round(lat, 4), "lon": round(lon, 4),
            "alt_km": round(alt, 1),
            "role": t.get("role", ""), "why": t.get("why", ""),
            "highlight": t.get("highlight", False),
        })
    return out


def next_passes(tles: list[dict], points: list[dict],
                roles: tuple = ("SAR-radar",),
                horizon_h: int = HORIZON_H) -> dict[str, dict]:
    """Nästa passage per punkt för satelliter med angivna roller.

    Returnerar {point_id: {satellite, in_minutes, at, distance_km}}.
    Endast highlight-satelliter propageras — att söka 24 h framåt för hela
    katalogen vore meningslöst dyrt.
    """
    from sgp4.api import Satrec
    sats = [t for t in tles if t.get("role") in roles]
    if not sats:
        return {}
    now = datetime.now(timezone.utc)
    best: dict[str, dict] = {}
    for t in sats:
        try:
            sat = Satrec.twoline2rv(t["tle1"], t["tle2"])
        except Exception:  # noqa: BLE001
            continue
        for step in range(0, horizon_h * 3600, STEP_S):
            when = now + timedelta(seconds=step)
            jd, fr = _jday(when)
            e, r, _v = sat.sgp4(jd, fr)
            if e != 0:
                continue
            lat, lon, _alt = _eci_to_geodetic(r[0], r[1], r[2], jd + fr)
            for p in points:
                # billig förfiltrering innan haversine
                if abs(lat - p["lat"]) > 4.0:
                    continue
                d = _haversine_km(lat, lon, p["lat"], p["lon"])
                if d > SWATH_KM:
                    continue
                cur = best.get(p["id"])
                if cur is None or step < cur["_step"]:
                    best[p["id"]] = {
                        "_step": step,
                        "satellite": t["name"],
                        "norad": t["norad"],
                        "in_minutes": round(step / 60),
                        "at": when.strftime("%Y-%m-%d %H:%M UTC"),
                        "distance_km": round(d),
                        "role": t.get("role", ""),
                    }
    for v in best.values():
        v.pop("_step", None)
    return best
