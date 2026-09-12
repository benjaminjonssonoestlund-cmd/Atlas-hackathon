"""Drivande & loitrande fartyg → råvarukoppling.

Tre observationsfamiljer slås ihop till EN lägesbild:

  1. EGEN LIVE-AIS: NavigationalStatus 2 ("ej under kontroll") och
     3 ("begränsad manöverförmåga") är fartygets EGEN deklaration av haveri/
     manöverproblem — hård signal, global täckning där vi hör AIS. Dessutom
     "misstänkt loitering": under gång (nav 0) men nästan stilla (0,3-1,5 kn)
     INUTI en chokepoint-zon — utanför zoner är samma mönster mest brus
     (fiske, lotsning), så vi flaggar det bara där det bär signal.
  2. GFW-EVENTS (satellit-AIS, 7 d): riktiga loitering-/omlastningsalgoritmer —
     fångar öppet hav som terrester AIS missar. Kräver gratis token.
  3. NGA-VARNINGAR: officiella "VESSEL ADRIFT/DERELICT/DISABLED"-rapporter.

Råvarukopplingen är mekanistisk och redovisas åt båda hållen (ingen låtsad
precision):

  * Drivande/havererade TANKERS nära ett oljesund → akut utbuds-/incidentrisk
    → historiskt Brent-POSITIV impuls (och försäkringspremier upp).
  * MÅNGA loitrande tankers i en region → flytande lager byggs upp →
    kortsiktigt utbudsÖVERHÄNG (Brent-negativt på kort sikt, klassisk
    contango-signal) — MEN utanför sanktionsnav (Malacka/Singapore, Danska
    sunden) är det ofta skuggflotta som VÄNTAR på köpare, dvs. utbud som
    inte når marknaden (positivt).
  * Drivande LASTFARTYG i containerleder → fraktrater (BDRY/BOAT) upp-risk.
"""

from __future__ import annotations

from .live import haversine_km

ZONE_KM = 250.0          # koppla observation till närmaste sund inom detta
SLOW_LO, SLOW_HI = 0.3, 1.5   # kn — "under gång men nästan stilla"
NAV_LABELS = {2: "ej under kontroll", 3: "begränsad manöver"}

# Sund där tanker-signalen är oljerelevant (matchar chokepoints.json)
OIL_CHOKEPOINTS = {"hormuz", "suez", "bab-el-mandeb", "malacca",
                   "danish-straits", "gibraltar", "bosporus"}
# Sanktions-/skuggflottenav: loitering här = last som väntar, inte lager-glut
SHADOW_HUBS = {"malacca", "danish-straits", "gibraltar"}


def own_drifters(items: list[dict], chokepoints: list[dict]) -> list[dict]:
    """Drivande/manöveroförmögna + zon-loitrande fartyg ur egna AIS-bilden."""
    out = []
    for v in items:
        nav = v.get("nav")
        sog = v.get("sog") or 0
        label = None
        if nav in NAV_LABELS:
            label = NAV_LABELS[nav]
        elif nav == 0 and SLOW_LO <= sog <= SLOW_HI:
            label = "misstänkt loitering"
        if label is None:
            continue
        cp_id, km = _nearest(v["lat"], v["lon"], chokepoints)
        if label == "misstänkt loitering" and cp_id is None:
            continue  # utanför zonerna är långsam gång mest brus
        out.append({
            "lat": v["lat"], "lon": v["lon"],
            "mmsi": v.get("mmsi"), "name": v.get("name") or "",
            "category": v.get("category") or "okänt",
            "sog": sog, "label": label,
            "chokepoint": cp_id, "km_to_chokepoint": round(km) if cp_id else None,
            "source": "egen AIS",
        })
    return out


def _nearest(lat: float, lon: float,
             chokepoints: list[dict]) -> tuple[str | None, float]:
    best, best_km = None, ZONE_KM
    for cp in chokepoints:
        if abs(cp["lat"] - lat) > 4.0 or abs(cp["lon"] - lon) > 5.0:
            continue
        km = haversine_km(lat, lon, cp["lat"], cp["lon"])
        if km < best_km:
            best, best_km = cp["id"], km
    return best, best_km


def _is_tanker(obs: dict) -> bool:
    t = (obs.get("category") or obs.get("vessel_type") or "").lower()
    return "tanker" in t


def assemble(items: list[dict], gfw_loitering: list[dict],
             gfw_encounters: list[dict], nga_warnings: list[dict],
             chokepoints: list[dict]) -> dict:
    """Hela drift-lägesbilden + råvarukoppling per sund."""
    own = own_drifters(items, chokepoints)

    def _tag(events: list[dict], kind: str) -> list[dict]:
        tagged = []
        for e in events:
            cp_id, km = _nearest(e["lat"], e["lon"], chokepoints)
            tagged.append({**e, "kind": kind, "chokepoint": cp_id,
                           "km_to_chokepoint": round(km) if cp_id else None})
        return tagged

    loit = _tag(gfw_loitering, "loitering")
    enc = _tag(gfw_encounters, "omlastning")
    warn = _tag(nga_warnings, "navvarning")

    cp_by_id = {cp["id"]: cp for cp in chokepoints}
    per_cp: dict[str, dict] = {}
    for obs_list, key in ((own, "own"), (loit, "loitering"),
                          (enc, "encounters"), (warn, "warnings")):
        for o in obs_list:
            cp_id = o.get("chokepoint")
            if not cp_id:
                continue
            entry = per_cp.setdefault(cp_id, {
                "own": 0, "own_tankers": 0, "loitering": 0,
                "loitering_tankers": 0, "encounters": 0, "warnings": 0})
            entry[key] += 1
            if key == "own" and _is_tanker(o):
                entry["own_tankers"] += 1
            if key == "loitering" and _is_tanker(o):
                entry["loitering_tankers"] += 1

    # Råvarukoppling i klartext per sund med aktivitet
    for cp_id, entry in per_cp.items():
        cp = cp_by_id.get(cp_id, {})
        notes = []
        symbols = [i["symbol"] for i in cp.get("instruments", [])]
        tankers = entry["own_tankers"] + entry["loitering_tankers"]
        if cp_id in OIL_CHOKEPOINTS and entry["own"] > 0 and entry["own_tankers"] > 0:
            notes.append(f"{entry['own_tankers']} manöverstörd(a) tanker(s) i zonen "
                         "— akut utbuds-/incidentrisk, historiskt Brent-positiv impuls")
        if entry["loitering_tankers"] >= 3:
            if cp_id in SHADOW_HUBS:
                notes.append(f"{entry['loitering_tankers']} loitrande tankers — "
                             "skuggflottemönster: last som väntar på köpare "
                             "(utbud som inte når marknaden)")
            else:
                notes.append(f"{entry['loitering_tankers']} loitrande tankers — "
                             "flytande lager byggs (kortsiktigt utbudsöverhäng)")
        if entry["encounters"] > 0 and cp_id in OIL_CHOKEPOINTS:
            notes.append(f"{entry['encounters']} omlastning(ar) till havs — "
                         "sanktionsflödessignal (GFW)")
        if entry["warnings"] > 0:
            notes.append(f"{entry['warnings']} officiell(a) sjövarning(ar) om "
                         "drivande/manöveroförmöget fartyg (NGA)")
        non_tanker_own = entry["own"] - entry["own_tankers"]
        if non_tanker_own >= 2:
            notes.append(f"{non_tanker_own} manöverstörda lastfartyg — "
                         "fraktrate-risk (BDRY/BOAT) om leden störs")
        entry["market_notes"] = notes
        entry["instruments"] = symbols
        entry["tankers_total"] = tankers

    return {
        "own": own[:400],
        "gfw_loitering": loit[:400],
        "gfw_encounters": enc[:200],
        "nga_warnings": warn[:100],
        "per_chokepoint": per_cp,
        "totals": {
            "own": len(own),
            "own_tankers": sum(1 for o in own if _is_tanker(o)),
            "gfw_loitering": len(loit),
            "gfw_encounters": len(enc),
            "nga_warnings": len(warn),
        },
    }
