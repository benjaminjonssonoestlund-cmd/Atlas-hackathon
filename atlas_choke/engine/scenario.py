"""Scenariomotor: "vad händer om flaskhals X stängs?"

Kontrafaktisk körning av samma kedja som live-analysen, men med ett sund
tvingat till full stress:

  1. Sundet sätts till stressnivå "allvarligt" → förseningsrisken räknas om
     med den riktiga `delays`-motorn (samma kod som live — inga särregler).
  2. Omvägens dygn hämtas ur lanes.json (Suez → +10-14 d runt Godahoppsudden).
  3. Handelsvägar som passerar sundet ärver risken (assess_lanes).
  4. Prisutfallet är sundets EGNA historiska analoger (event-studien) — vi
     hittar inte på siffror, vi visar vad marknaden gjorde förra gången.
  5. Godahoppsudden får omdirigeringspåslag när Suez/Bab-el-Mandeb stängs
     (mekanismen som faktiskt inträffade 2023-24).

Allt märks `simulated: true` hela vägen ut i UI:t.
"""

from __future__ import annotations

from . import delays as delays_engine

REROUTE_TO_CAPE = {"suez", "bab-el-mandeb"}
FORCED_STRESS = {"level": "allvarligt", "ongoing_since": "SIMULERAD",
                 "score": 100.0, "directed_z": 5.0}


def run(cp_id: str, chokepoints: list[dict], lanes: list[dict],
        alternatives: dict, bundle: dict, weather: dict,
        live: dict) -> dict:
    """Kör scenariot för `cp_id` och returnera hela den påverkade bilden."""
    target = next((c for c in chokepoints if c["id"] == cp_id), None)
    if target is None:
        return {"error": "okänt sund", "id": cp_id}

    by_id = {c["id"]: c for c in bundle.get("chokepoints", [])}
    per_cp: dict[str, dict] = {}
    for cp in chokepoints:
        forced = None
        if cp["id"] == cp_id:
            forced = FORCED_STRESS
        elif cp_id in REROUTE_TO_CAPE and cp["id"] == "cape-good-hope":
            # omdirigeringsmottagaren belastas när huvudleden stängs
            forced = {"level": "förhöjt", "ongoing_since": "SIMULERAD",
                      "score": 75.0, "directed_z": 2.5}
        else:
            forced = (by_id.get(cp["id"]) or {}).get("stress")
        per_cp[cp["id"]] = delays_engine.assess_chokepoint(
            cp, weather.get(cp["id"], {}), [],
            live.get(cp["id"]), forced, alternatives.get(cp["id"]))

    lane_rows = delays_engine.assess_lanes(lanes, per_cp)
    affected = [l for l in lane_rows if cp_id in (l.get("via") or [])]

    # Prisscenario = sundets egna historiska analoger (ingen ny modell)
    entry = by_id.get(cp_id) or {}
    fc = entry.get("forecast") or {}
    instruments = []
    for inst in fc.get("instruments", []):
        h = inst.get("horizons") or {}
        row = {"symbol": inst["symbol"], "name": inst["name"],
               "why": inst.get("why", ""), "confidence": inst.get("confidence")}
        for key in ("5", "10", "20", 5, 10, 20):
            cell = h.get(key)
            if cell and cell.get("analog"):
                row.setdefault("analog", {})[str(key)] = cell["analog"]
        instruments.append(row)

    return {
        "simulated": True,
        "chokepoint": {"id": cp_id, "name": target["name"],
                       "trade_note": target.get("trade_note", "")},
        "alternative": alternatives.get(cp_id),
        "per_chokepoint": per_cp,
        "lanes": lane_rows,
        "affected_lanes": [{"id": l["id"], "name": l["name"],
                            "teu_share_pct": l.get("teu_share_pct"),
                            "risk_level": l["risk_level"],
                            "delay_days": l.get("delay_days")}
                           for l in affected],
        "teu_at_risk_pct": round(sum(l.get("teu_share_pct") or 0 for l in affected), 1),
        "instruments": instruments,
        "n_episodes": len(entry.get("episodes", [])),
    }
