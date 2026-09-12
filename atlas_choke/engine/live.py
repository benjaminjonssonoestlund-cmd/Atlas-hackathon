"""Live-lägesbild per chokepoint ur AIS-snapshotet — "nuet" som historiken jämförs mot.

Rollfördelningen i systemet (användarens prioritering):
  * LIVE-AIS   → vad händer JUST NU: fartyg i zonen, ankrade köer, fartmönster.
  * PortWatch  → normen: vad som är normalt för sundet (dagliga passager, 7 år).
  * Event-studien → analogerna: vad liknande lägen historiskt gjort med priser.

Kösignalen använder NavigationalStatus (1 = till ankars, 5 = förtöjd) när den
finns — ankrad utanför ett sund är RIKTIG kö; låg fart ensam kan vara fiske
eller manöver. Fallback: fart < 0,5 kn.

Egen historik: varje anrop kan appendas till en ring-buffert på disk
(live_history.json) så att "nu vs för en timme/vecka sedan" växer fram från
dag ett — det är så live-metriken får sin egen baslinje över tid.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import time
from pathlib import Path

ZONE_RADIUS_KM = 90.0        # standardzon runt ett sund
CAPE_RADIUS_KM = 150.0       # Godahoppsudden är en vidsträckt passage
ANCHORED_NAV = {1, 5}        # till ankars / förtöjd
STATIONARY_KN = 0.5

HISTORY_PATH = Path(os.environ.get("ATLAS_LIVE_HISTORY")
                    or (Path(__file__).resolve().parent.parent / "data" / "live_history.json"))
HISTORY_MAX = 4032           # 14 dagar à 5-min-poster
HISTORY_MIN_GAP_S = 240      # appenda max var 4:e minut


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _is_anchored(v: dict) -> bool:
    nav = v.get("nav")
    if nav in ANCHORED_NAV:
        return True
    if nav is None:
        return (v.get("sog") or 0) < STATIONARY_KN
    return False


# Last-proxy (IMF WP/19/275, Arslanalp/Marini/Tumbarello; öppen replikering i
# Världsbankens pacific-observatory): deplacement ≈ L × B × djupgående × ρ × c_b.
# ρ = 1,025 t/m³ (havsvatten), c_b ≈ 0,70 (typiskt blockkoefficient handelsfartyg).
# Grovt men ärligt märkt — går från "antal skrov" till "ton i vattnet".
SEAWATER_T_M3 = 1.025
BLOCK_COEFF = 0.70


def _displacement_t(v: dict) -> float | None:
    loa, beam, draught = v.get("loa_m"), v.get("beam_m"), v.get("draught_m")
    if not (loa and beam and draught):
        return None
    return loa * beam * draught * SEAWATER_T_M3 * BLOCK_COEFF


def live_stats(items: list[dict], chokepoints: list[dict]) -> dict:
    """Per sund: fartyg i zonen nu, ankrade, kategorier, fart, tonnage-proxy."""
    per_cp = {}
    for cp in chokepoints:
        radius = CAPE_RADIUS_KM if cp["id"] == "cape-good-hope" else ZONE_RADIUS_KM
        lat0, lon0 = cp["lat"], cp["lon"]
        in_zone, anchored, moving_sogs = [], 0, []
        cats: dict[str, int] = {}
        disp_sum, disp_n = 0.0, 0
        for v in items:
            # snabb för-filtrering innan haversine (samma trick som atlas-earth)
            if abs(v["lat"] - lat0) > 2.5 or abs(v["lon"] - lon0) > 3.5:
                continue
            if haversine_km(lat0, lon0, v["lat"], v["lon"]) > radius:
                continue
            in_zone.append(v)
            cats[v.get("category") or "okänt"] = cats.get(v.get("category") or "okänt", 0) + 1
            if _is_anchored(v):
                anchored += 1
            elif (v.get("sog") or 0) >= STATIONARY_KN:
                moving_sogs.append(v["sog"])
            d = _displacement_t(v)
            if d:
                disp_sum += d
                disp_n += 1
        per_cp[cp["id"]] = {
            "ships": len(in_zone),
            "anchored": anchored,
            "anchored_share": round(anchored / len(in_zone), 2) if in_zone else None,
            "median_sog_kn": round(statistics.median(moving_sogs), 1) if moving_sogs else None,
            "by_category": dict(sorted(cats.items(), key=lambda kv: -kv[1])[:4]),
            "radius_km": radius,
            "est_tonnage_kt": round(disp_sum / 1000) if disp_n else None,
            "tonnage_coverage": round(disp_n / len(in_zone), 2) if in_zone else None,
        }
    totals = {
        "vessels": len(items),
        "anchored": sum(1 for v in items if _is_anchored(v)),
    }
    return {"per_chokepoint": per_cp, "totals": totals, "ts": time.time()}


class TransitCounter:
    """Live-passageräknare per zon (hormuz.now-metoden, förenklad).

    Ett INTRÄDE räknas när ett fartyg i rörelse (sog ≥ tröskeln) syns i zonen
    utan att ha varit där i föregående observation. Riktning ignoreras —
    poängen är jämförelsen "livetakt vs PortWatch-norm", inte exakt
    trafiksplit. Tillståndet är processlokalt: räknaren gäller sedan
    serverstart och märks så i UI:t.
    """

    def __init__(self) -> None:
        self.inside: dict[str, set] = {}
        self.entries: dict[str, int] = {}
        self.started = time.time()

    def update(self, items: list[dict], chokepoints: list[dict]) -> dict[str, int]:
        for cp in chokepoints:
            radius = CAPE_RADIUS_KM if cp["id"] == "cape-good-hope" else ZONE_RADIUS_KM
            lat0, lon0 = cp["lat"], cp["lon"]
            now_inside = set()
            for v in items:
                if abs(v["lat"] - lat0) > 2.5 or abs(v["lon"] - lon0) > 3.5:
                    continue
                if haversine_km(lat0, lon0, v["lat"], v["lon"]) <= radius:
                    now_inside.add(v["mmsi"])
            prev = self.inside.get(cp["id"], set())
            moving = {v["mmsi"] for v in items
                      if (v.get("sog") or 0) >= STATIONARY_KN}
            new_entries = len((now_inside - prev) & moving) if prev else 0
            self.entries[cp["id"]] = self.entries.get(cp["id"], 0) + new_entries
            self.inside[cp["id"]] = now_inside
        return dict(self.entries)


# ---------- egen live-historik (ring-buffert på disk) ----------

def record(stats: dict) -> None:
    """Appenda en kompakt post — kastar aldrig (historiken är best effort)."""
    try:
        hist = _read_raw()
        if hist and stats["ts"] - hist[-1]["ts"] < HISTORY_MIN_GAP_S:
            return
        hist.append({
            "ts": round(stats["ts"]),
            "cp": {cp_id: [s["ships"], s["anchored"]]
                   for cp_id, s in stats["per_chokepoint"].items()},
            "total": stats["totals"]["vessels"],
        })
        hist = hist[-HISTORY_MAX:]
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(hist), encoding="utf-8")
        os.replace(tmp, HISTORY_PATH)
    except Exception:  # noqa: BLE001
        pass


def _read_raw() -> list[dict]:
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def trend(cp_id: str, hours: float = 24.0) -> dict | None:
    """Nu vs medel de senaste `hours` — kräver minst en timmes egen historik."""
    hist = _read_raw()
    if len(hist) < 5:
        return None
    now = hist[-1]
    cutoff = now["ts"] - hours * 3600
    window = [h for h in hist[:-1] if h["ts"] >= cutoff and cp_id in h.get("cp", {})]
    if len(window) < 4 or now["ts"] - window[0]["ts"] < 3600:
        return None
    cur = now.get("cp", {}).get(cp_id)
    if not cur:
        return None
    ships_avg = statistics.fmean(h["cp"][cp_id][0] for h in window)
    anch_avg = statistics.fmean(h["cp"][cp_id][1] for h in window)
    return {
        "ships_now": cur[0], "ships_avg": round(ships_avg, 1),
        "anchored_now": cur[1], "anchored_avg": round(anch_avg, 1),
        "window_h": round((now["ts"] - window[0]["ts"]) / 3600, 1),
    }
