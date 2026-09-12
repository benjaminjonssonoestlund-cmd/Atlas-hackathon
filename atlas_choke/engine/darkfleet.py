"""Mörka fartyg (AIS-gap) och omlastningar till havs — egen detektion.

Trösklarna är hämtade från thanderoy/ais-tracker (Go, maritime intelligence
backend) som kör dem i produktion; logiken här är egen och arbetar på vårt
snapshot i stället för PostgreSQL.

  AIS-GAP ("dark vessel"): 6 h tystnad öppnar ett gap, >72 h räknas fartyget
  som borta (inte mörkt). Fartyg i hamn undantas — de slocknar legitimt.
  Att dyka upp igen >50 km bort är det intressanta fallet.

  OMLASTNING (STS): två fartyg inom 500 m, båda under 3 knop, i minst 30 min.

VARFÖR: att stänga av AIS är den klassiska metoden för att dölja sanktionerad
last. Ett mörkt TANKFARTYG vid ett oljesund, särskilt under högriskflagg, är
den starkaste enskilda sanktionsindikatorn öppna data kan ge — och den blir
verifierbar när SAR-radarn ser ett skrov där ingen AIS finns.

ÄRLIGHET: terrester AIS har täckningshål. Ett "gap" mitt i Indiska oceanen är
oftast bara mottagarbrist, inte uppsåt. Därför kräver detektorn att fartyget
senast sågs INOM en chokepoint-zon (där täckningen är god) och rapporterar
alltid var det försvann.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .live import haversine_km

SEEN_PATH = Path(os.environ.get("ATLAS_SEEN_STORE")
                 or (Path(__file__).resolve().parent.parent / "data" / "vessel_seen.json"))

GAP_OPEN_H = 6.0        # tystnad längre än så öppnar ett gap
GAP_MAX_H = 72.0        # längre än så: borta, inte mörkt
REAPPEAR_FAR_KM = 50.0  # dyker upp längre bort än så = intressant
ZONE_KM = 120.0         # senast sedd inom detta av ett sund (god täckning)

STS_MAX_M = 500.0       # avstånd mellan fartygen
STS_MAX_KN = 3.0        # båda långsammare än så
STS_MIN_MIN = 30.0      # måste hålla ihop så länge
STS_MIN_PORT_KM = 25.0  # måste vara så här långt från närmaste hamn

# Fartyg vid kaj ligger per definition tätt ihop och stilla. Utan det här
# filtret rapporterade detektorn ~6 000 "omlastningar" i Rotterdams hamn —
# ais-tracker undviker samma fälla genom att slå bort fartyg i hamn.
MOORED_NAV = 5

_pairs: dict[tuple, float] = {}   # (mmsi_a, mmsi_b) → första observation


def load_seen() -> dict:
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_seen(seen: dict) -> None:
    try:
        tmp = SEEN_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(seen, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, SEEN_PATH)
    except OSError:
        pass


def update_seen(items: list[dict], chokepoints: list[dict]) -> dict:
    """Uppdatera senast-sedd-registret. Kompakt form: [ts, lat, lon, kat, flagg, namn, cp]."""
    seen = load_seen()
    now = time.time()
    for v in items:
        mmsi = str(v.get("mmsi"))
        lat, lon = v.get("lat"), v.get("lon")
        if not mmsi or lat is None:
            continue
        cp_id = ""
        for cp in chokepoints:
            if abs(lat - cp["lat"]) > 2.0 or abs(lon - cp["lon"]) > 3.0:
                continue
            if haversine_km(cp["lat"], cp["lon"], lat, lon) <= ZONE_KM:
                cp_id = cp["id"]
                break
        flag = (v.get("flag") or {}).get("iso2", "")
        seen[mmsi] = [round(now), round(lat, 4), round(lon, 4),
                      v.get("category") or "", flag,
                      (v.get("name") or "")[:28], cp_id]
    # rensa poster äldre än en vecka så filen inte växer i evighet
    cutoff = now - 7 * 86400
    for m in [m for m, r in seen.items() if r[0] < cutoff]:
        del seen[m]
    save_seen(seen)
    return seen


def dark_vessels(current: list[dict], seen: dict) -> list[dict]:
    """Fartyg som tystnat 6-72 h efter att senast ha setts i en chokepoint-zon."""
    live_mmsi = {str(v.get("mmsi")) for v in current}
    now = time.time()
    out = []
    for mmsi, r in seen.items():
        if mmsi in live_mmsi:
            continue
        ts, lat, lon, cat, flag, name, cp_id = (r + [""] * 7)[:7]
        if not cp_id:
            continue                       # tystnad utanför zon = täckningshål
        gap_h = (now - ts) / 3600
        if not (GAP_OPEN_H <= gap_h <= GAP_MAX_H):
            continue
        out.append({
            "mmsi": mmsi, "name": name or f"MMSI {mmsi}",
            "category": cat, "flag": flag,
            "lat": lat, "lon": lon, "chokepoint": cp_id,
            "gap_hours": round(gap_h, 1),
            "tanker": cat == "tanker",
        })
    out.sort(key=lambda d: (not d["tanker"], -d["gap_hours"]))
    return out


def sts_candidates(items: list[dict], ports: list[dict] | None = None,
                   chokepoints: list[dict] | None = None) -> list[dict]:
    """Fartygspar som ligger still tätt ihop TILL HAVS — möjliga omlastningar.

    TVÅ filter krävs för att signalen ska bli meningsfull, båda upptäckta
    genom att titta på utfallet:

    * Hamnar utesluts (≤25 km): vid kaj ligger fartyg alltid tätt och stilla
      — utan filtret rapporterades ~6 000 "omlastningar" i Rotterdams hamn.
    * Endast inne i bevakade chokepoint-zoner: pråmar på Rhen, Elbe och
      holländska kanaler ligger också tätt och stilla, långt från varje
      SEAhamn. Det är dessutom bara omlastningar vid flaskhalsarna som är
      relevanta för det här systemet.
    """
    slow = [v for v in items
            if v.get("lat") is not None and (v.get("sog") or 0) <= STS_MAX_KN
            and v.get("nav") != MOORED_NAV
            and (v.get("category") in ("tanker", "lastfartyg"))]
    if chokepoints:
        def in_zone(v) -> bool:
            for cp in chokepoints:
                if abs(v["lat"] - cp["lat"]) > 2.0 or abs(v["lon"] - cp["lon"]) > 3.0:
                    continue
                if haversine_km(cp["lat"], cp["lon"], v["lat"], v["lon"]) <= ZONE_KM:
                    return True
            return False

        slow = [v for v in slow if in_zone(v)]
    if ports:
        # grovt rutnätsindex över hamnar → O(1)-uppslag per fartyg
        pgrid: dict[tuple, list[tuple]] = {}
        for p in ports:
            if p.get("lat") is None:
                continue
            pgrid.setdefault((round(p["lat"] * 2), round(p["lon"] * 2)),
                             []).append((p["lat"], p["lon"]))

        def near_port(v) -> bool:
            k = (round(v["lat"] * 2), round(v["lon"] * 2))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for plat, plon in pgrid.get((k[0] + dy, k[1] + dx), ()):
                        if haversine_km(v["lat"], v["lon"], plat, plon) <= STS_MIN_PORT_KM:
                            return True
            return False

        slow = [v for v in slow if not near_port(v)]
    # rutnätsindex så vi slipper 27k² jämförelser
    grid: dict[tuple, list[dict]] = {}
    for v in slow:
        key = (round(v["lat"] * 20), round(v["lon"] * 20))   # ~5 km-rutor
        grid.setdefault(key, []).append(v)

    now = time.time()
    found = []
    seen_pairs = set()
    for key, bucket in grid.items():
        neigh = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                neigh.extend(grid.get((key[0] + dy, key[1] + dx), []))
        for a in bucket:
            for b in neigh:
                if a["mmsi"] >= b["mmsi"]:
                    continue
                pair = (a["mmsi"], b["mmsi"])
                if pair in seen_pairs:
                    continue
                dist_m = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) * 1000
                if dist_m > STS_MAX_M:
                    continue
                seen_pairs.add(pair)
                first = _pairs.setdefault(pair, now)
                held_min = (now - first) / 60
                if held_min < STS_MIN_MIN:
                    continue
                found.append({
                    "a": {"mmsi": a["mmsi"], "name": a.get("name") or "",
                          "category": a.get("category"),
                          "flag": (a.get("flag") or {}).get("iso2", "")},
                    "b": {"mmsi": b["mmsi"], "name": b.get("name") or "",
                          "category": b.get("category"),
                          "flag": (b.get("flag") or {}).get("iso2", "")},
                    "lat": round((a["lat"] + b["lat"]) / 2, 4),
                    "lon": round((a["lon"] + b["lon"]) / 2, 4),
                    "distance_m": round(dist_m),
                    "held_minutes": round(held_min),
                    "both_tankers": a.get("category") == b.get("category") == "tanker",
                })
    # glöm par som inte längre ligger ihop
    for p in [p for p in _pairs if p not in seen_pairs]:
        del _pairs[p]
    found.sort(key=lambda f: (not f["both_tankers"], -f["held_minutes"]))
    return found
