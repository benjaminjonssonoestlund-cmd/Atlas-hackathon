"""IDAG-briefen: automatgenererad lägesrapport ur systemets samlade data.

Ren mallogik (ingen LLM — deterministisk och demosäker): topp-stress,
pågående episoder, förseningsrisker, största marknadsrörelser, live-läget.
Allt är siffror systemet redan äger; briefen är bara redigering.
"""

from __future__ import annotations

import time


def build(items: list[dict], delays: dict, live: dict,
          quotes: list[dict], sar: dict) -> dict:
    """items = overview-raderna; delays = /api/delays-svaret; live = /api/live;
    quotes = marknadsraderna; sar = /api/sar-svaret."""
    sections: list[dict] = []

    # --- Stressläget ---
    stressed = sorted([i for i in items if (i.get("score") or 0) >= 65],
                      key=lambda i: -(i["score"] or 0))
    ongoing = [i for i in items if i.get("active")]
    if stressed:
        rows = [f"{i['name']}: {i['score']}/100 ({i['level']})"
                + (f", tonnage {i['deviation_pct']:+.0f} % mot baslinje"
                   if i.get("deviation_pct") is not None else "")
                for i in stressed[:4]]
        sections.append({"title": "Stressade flaskhalsar",
                         "rows": rows})
    headline = (f"{len(stressed)} av {len(items)} flaskhalsar stressade, "
                f"{len(ongoing)} pågående störningsepisoder"
                if stressed else "Världshandelns flaskhalsar i normalläge")

    # --- Förseningsrisk ---
    per_cp = delays.get("per_chokepoint", {})
    risky = sorted(((cp_id, r) for cp_id, r in per_cp.items()
                    if r.get("level") in ("förhöjd", "hög")),
                   key=lambda t: -t[1]["score"])
    if risky:
        name_by_id = {i["id"]: i["name"] for i in items}
        rows = []
        for cp_id, r in risky[:4]:
            d = r.get("delay_days")
            rows.append(f"{name_by_id.get(cp_id, cp_id)}: {r['level']} "
                        f"({r['score']}/100)"
                        + (f", bedömd försening +{d[0]}-{d[1]} dygn" if d else ""))
        sections.append({"title": "Förseningsrisk (väder, larm, kö, episoder)",
                         "rows": rows})

    # --- Handelsvägar ---
    lanes_risky = [l for l in delays.get("lanes", [])
                   if l.get("risk_level") in ("förhöjd", "hög")]
    if lanes_risky:
        rows = [f"{l['name']}: {l['risk_level']} via {l.get('worst_via', '?')}"
                + (f" (~{l['teu_share_pct']} % av global TEU)"
                   if l.get("teu_share_pct") else "")
                for l in lanes_risky[:4]]
        sections.append({"title": "Påverkade handelsvägar", "rows": rows})

    # --- Marknaden ---
    movers = sorted([q for q in quotes if q.get("change_pct") is not None],
                    key=lambda q: -abs(q["change_pct"]))[:4]
    if movers:
        rows = [f"{q['name']}: {q['price']} ({q['change_pct']:+.1f} %)"
                for q in movers]
        sections.append({"title": "Största rörelser, kopplade instrument",
                         "rows": rows})

    # --- Live-bilden ---
    totals = live.get("totals", {})
    lrows = []
    if totals.get("vessels"):
        lrows.append(f"{totals['vessels']:,} fartyg i live-bilden "
                     f"({totals.get('anchored', 0):,} ankrade/förtöjda)".replace(",", " "))
    queues = sorted(((cp_id, s) for cp_id, s in live.get("per_chokepoint", {}).items()
                     if (s.get("anchored") or 0) >= 5),
                    key=lambda t: -t[1]["anchored"])
    name_by_id = {i["id"]: i["name"] for i in items}
    for cp_id, s in queues[:3]:
        lrows.append(f"Kö vid {name_by_id.get(cp_id, cp_id)}: "
                     f"{s['anchored']} ankrade av {s['ships']} i zonen")
    scenes = sar.get("scenes") or []
    for sc in scenes:
        lrows.append(f"Radar (Sentinel-1, {str(sc.get('scene_time', ''))[:10]}): "
                     f"{sc.get('n', 0)} detektioner i {sc.get('aoi', '?')}")
    if lrows:
        sections.append({"title": "Live just nu", "rows": lrows})

    return {"headline": headline, "generated": time.time(),
            "sections": sections,
            "footer": "Automatgenererad ur PortWatch, AIS, Open-Meteo, GDACS, "
                      "Yahoo och Sentinel-1. Ej finansiell rådgivning."}
