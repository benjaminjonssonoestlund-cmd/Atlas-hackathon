"""Förseningsrisk per chokepoint och handelsväg.

Väger ihop fyra oberoende signalfamiljer till en riskpoäng 0-100 med
läsbara skäl (varje poängbidrag motiveras i klartext — juryn ska kunna
granska bedömningen, inte lita på en svart låda):

  1. VÄDER (Open-Meteo, nu + 48 h prognos): hård vind/byar stänger konvojer
     (Suez stänger vid sandstorm/hård vind, Bosporen vid stark sydvästvind,
     Panama begränsar vid dimma); hög sjö saktar transiter och stoppar lotsning.
  2. KATASTROFLARM (GDACS): tropisk cyklon/tsunami nära sundet — RED inom
     ~800 km eller ORANGE inom ~400 km är transitpåverkande.
  3. LIVE-KÖ (AIS): ankrade fartyg klart över det normala = försening pågår.
  4. STRUKTURELL STRESS (PortWatch-motorn): pågående episod = redan stört.

Nivåer: <25 låg · 25-55 förhöjd · >55 hög.
Förseningsestimat: väder/kö ger korta dygnsintervall; pågående episod ger
omvägsalternativets intervall ur lanes.json (t.ex. Suez → +10-14 d runt
Godahoppsudden). En handelsvägs risk = värsta via-sundets risk.
"""

from __future__ import annotations

import math

# vind/byar i m/s, våg i meter — trösklar för transitpåverkan
GUST_HIGH, GUST_ELEV = 20.0, 14.0
WAVE_HIGH, WAVE_ELEV = 5.0, 3.0
CYCLONE_KM_RED, CYCLONE_KM_ORANGE = 800.0, 400.0
QUEUE_HIGH_SHARE, QUEUE_MIN_SHIPS = 0.55, 10

LEVELS = [(55.0, "hög"), (25.0, "förhöjd"), (0.0, "låg")]


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def classify(score: float) -> str:
    for threshold, label in LEVELS:
        if score >= threshold:
            return label
    return "låg"


DISRUPTION_KM = 300.0


def assess_chokepoint(cp: dict, weather: dict, gdacs_alerts: list[dict],
                      live: dict | None, stress: dict | None,
                      alternative: dict | None,
                      disruptions: list[dict] | None = None) -> dict:
    """Riskbedömning för ETT sund. Alla indata får vara tomma/None."""
    score = 0.0
    reasons: list[dict] = []
    delay_lo, delay_hi = 0, 0

    # --- 0. Rapporterade hamnstörningar nära sundet (PortWatch/GDACS) ---
    for d in disruptions or []:
        km = _haversine_km(cp["lat"], cp["lon"], d["lat"], d["lon"])
        if km > DISRUPTION_KM:
            continue
        red = d.get("alertlevel") == "RED"
        pts = 15 if red else 8
        score += pts
        reasons.append({"pts": pts, "txt": f"Hamnstörning: {d.get('name', '?')} "
                        f"({d.get('alertlevel', '?')}) {km:.0f} km bort — "
                        f"{d.get('n_ports') or '?'} hamnar berörda"})
        delay_hi = max(delay_hi, 2 if red else 1)
        break  # en räcker — flera larm i samma kluster ska inte stapla poäng

    # --- 1. Väder (prognosens max väger — risken ska flaggas i förväg) ---
    gust = max(weather.get("gust_ms") or 0, weather.get("gust_max48_ms") or 0)
    wave = max(weather.get("wave_m") or 0, weather.get("wave_max48_m") or 0)
    if gust >= GUST_HIGH:
        score += 30
        reasons.append({"pts": 30, "txt": f"Byar upp till {gust:.0f} m/s inom 48 h — "
                        "konvoj-/lotsstopp sannolikt"})
        delay_lo, delay_hi = max(delay_lo, 1), max(delay_hi, 3)
    elif gust >= GUST_ELEV:
        score += 14
        reasons.append({"pts": 14, "txt": f"Byar {gust:.0f} m/s inom 48 h — "
                        "begränsningar möjliga"})
        delay_hi = max(delay_hi, 1)
    if wave >= WAVE_HIGH:
        score += 22
        reasons.append({"pts": 22, "txt": f"Våghöjd upp till {wave:.1f} m inom 48 h — "
                        "transitstopp för mindre tonnage"})
        delay_lo, delay_hi = max(delay_lo, 1), max(delay_hi, 2)
    elif wave >= WAVE_ELEV:
        score += 10
        reasons.append({"pts": 10, "txt": f"Våghöjd {wave:.1f} m inom 48 h — långsammare transiter"})

    # --- 2. GDACS-larm nära sundet ---
    for a in gdacs_alerts:
        if a.get("event_code") not in ("TC", "TS"):
            continue
        km = _haversine_km(cp["lat"], cp["lon"], a["lat"], a["lon"])
        level = a.get("alertlevel")
        if level == "RED" and km <= CYCLONE_KM_RED:
            score += 35
            reasons.append({"pts": 35, "txt": f"{a['kind']} {a['name']} (RÖD) "
                            f"{km:.0f} km bort"})
            delay_lo, delay_hi = max(delay_lo, 2), max(delay_hi, 5)
        elif level in ("RED", "ORANGE") and km <= CYCLONE_KM_ORANGE:
            score += 18
            reasons.append({"pts": 18, "txt": f"{a['kind']} {a['name']} ({level}) "
                            f"{km:.0f} km bort"})
            delay_hi = max(delay_hi, 2)

    # --- 3. Live-kö ur AIS ---
    if live:
        ships, anch = live.get("ships") or 0, live.get("anchored") or 0
        share = anch / ships if ships else 0
        if ships >= QUEUE_MIN_SHIPS and share >= QUEUE_HIGH_SHARE:
            score += 18
            reasons.append({"pts": 18, "txt": f"{anch} av {ships} fartyg i zonen "
                            "ligger för ankar (live-AIS) — kö pågår"})
            delay_lo, delay_hi = max(delay_lo, 1), max(delay_hi, 3)

    # --- 4. Strukturell stress (pågående episod, viktad efter allvar) ---
    # Ett stängt/allvarligt stört sund ÄR hög förseningsrisk i sig — att
    # Hormuz "bara" fick förhöjd med platt +30 var en felkalibrering.
    if stress and stress.get("ongoing_since"):
        lvl = stress.get("level") or ""
        pts = 55 if lvl == "allvarligt" else 35 if lvl == "förhöjt" else 30
        score += pts
        reasons.append({"pts": pts, "txt": "Pågående störningsepisod sedan "
                        f"{stress['ongoing_since']} (PortWatch: {lvl or 'okänd nivå'})"})
        if alternative and alternative.get("delay_days"):
            lo, hi = alternative["delay_days"]
            delay_lo, delay_hi = max(delay_lo, lo), max(delay_hi, hi)
            reasons.append({"pts": 0, "txt": f"Omväg: {alternative['route']} "
                            f"(+{lo}-{hi} dygn)"})

    score = min(100.0, score)
    return {
        "score": round(score),
        "level": classify(score),
        "reasons": reasons,
        "delay_days": [delay_lo, delay_hi] if delay_hi else None,
        "weather": {k: weather.get(k) for k in
                    ("wind_ms", "gust_ms", "wave_m",
                     "gust_max48_ms", "wave_max48_m") if weather.get(k) is not None},
    }


def assess_lanes(lanes: list[dict], per_cp: dict[str, dict]) -> list[dict]:
    """Handelsvägens risk = värsta via-sundets risk (kedjan är aldrig starkare
    än sin svagaste länk). Returnerar lanes berikade med risk + värsta länk."""
    out = []
    for lane in lanes:
        worst_id, worst = None, None
        for via in lane.get("via", []):
            r = per_cp.get(via)
            if r and (worst is None or r["score"] > worst["score"]):
                worst_id, worst = via, r
        out.append({
            "id": lane["id"], "name": lane["name"],
            "teu_share_pct": lane.get("teu_share_pct"),
            "waypoints": lane["waypoints"],
            "via": lane.get("via", []),
            "risk_score": worst["score"] if worst else 0,
            "risk_level": worst["level"] if worst else "låg",
            "worst_via": worst_id,
            "delay_days": worst.get("delay_days") if worst else None,
        })
    return out
