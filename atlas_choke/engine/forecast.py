"""Prognos: nuläge + historiska analoger → scenarioband per instrument.

Logiken är medvetet enkel och ärlig:

  * Är sundet stressat NU (riktad z ≥ ACTIVE_Z)? Då är prognosen de
    empiriska kvantilerna av vad de kopplade instrumenten historiskt gjort
    efter liknande episodstarter (event-studiens poolade band).
  * Är läget normalt? Då är prognosen instrumentets OVILLKORADE band —
    och det sägs rakt ut ("ingen aktiv störning, basnivå").
  * Edge-mått: skillnaden mellan analog-medianen och bas-medianen. Det är
    den siffra en jury ska titta på — utan den är banden bara tapeter.

Konfidensen graderas på antal analoger: <3 analoger → "svag" (banden visas
men flaggas), annars "ok". Inga påhittade sannolikheter.
"""

from __future__ import annotations

ACTIVE_Z = 1.5
HORIZONS = (5, 10, 20)


def outlook(assessment: dict | None, study: dict,
            base_stats: dict[str, dict[int, dict]],
            instruments: list[dict]) -> dict:
    """Prognosobjekt för ETT sund.

    assessment: stress.assess()-resultatet (eller None).
    study: events.event_study()-resultatet för sundet.
    base_stats: {symbol: unconditional_stats(...)}.
    instruments: chokepoints.json-listan [{symbol, name, why}].
    """
    # Aktiv = pågående episod (frusen baslinje) ELLER dagens z över tröskeln.
    # Enbart z:an räcker inte: mitt i en lång störning kan dagsvärdet dippa
    # under tröskeln utan att episoden är över (Malacka aug 2026).
    active = bool(assessment and (assessment.get("ongoing_since")
                                  or assessment["directed_z"] >= ACTIVE_Z))
    n_episodes = len(study.get("episodes", []))
    rows = []
    for inst in instruments:
        sym = inst["symbol"]
        pooled = study.get("pooled", {}).get(sym, {})
        base = base_stats.get(sym, {})
        horizons = {}
        for h in HORIZONS:
            p = pooled.get(h)
            b = base.get(h)
            entry: dict = {}
            if b:
                entry["base"] = b
            if p:
                entry["analog"] = p
                if b:
                    entry["edge_median"] = round(p["median"] - b["median"], 2)
            if entry:
                horizons[h] = entry
        confidence = ("svag" if n_episodes < 3 else "ok") if pooled else "ingen"
        rows.append({
            "symbol": sym, "name": inst["name"], "why": inst.get("why", ""),
            "confidence": confidence,
            "horizons": horizons,
        })
    return {
        "active": active,
        "directed_z": assessment["directed_z"] if assessment else None,
        "n_episodes": n_episodes,
        "mode": "analog" if active else "basnivå",
        "note": ("Aktiv störning: banden är empiriska kvantiler av utfallen efter "
                 f"{n_episodes} historiska episodstarter i detta sund."
                 if active else
                 "Ingen aktiv störning: banden visar instrumentets normala "
                 "fördelning (basnivå). Analogbanden aktiveras när riktad z ≥ 1,5."),
        "instruments": rows,
    }
