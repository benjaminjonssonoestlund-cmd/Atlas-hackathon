"""Flaggstat per fartyg + skuggflotte-exponering vid oljesunden.

MMSI:s tre första siffror (MID) är fartygets flaggstat — en identitet vi
tidigare kastade bort. Idén att berika på MID kom från en genomgång av
FlightAirMap; själva tabellen är dock byggd ur ITU:s officiella
MID-allokering (publik standard), inte kopierad därifrån — FlightAirMap är
AGPL och ska inte smitta det här projektet.

VARFÖR DET ÄR EN FINANSSIGNAL: sanktionerad olja flyttas av en "skuggflotta"
som seglar under små registreringsflaggor vars tankflottor exploderade efter
2022 (Gabon, Kamerun, Cooköarna, Komorerna, Palau, Barbados m.fl.). En
onormal andel högrisk-flaggade TANKERS vid ett oljesund är därför en
observerbar indikator på sanktionsflöden — samma mekanism som våra
loitering-/omlastningssignaler, men från identitetshållet.

ÄRLIGHET: flagg ≠ bevis. Panama och Liberia är världens två största
registreringar och flaggar tusentals helt legitima fartyg. Signalen är
ANDELEN högriskflagg bland tankers i zonen jämfört med den globala
tankerflottans normalfördelning — inte en anklagelse mot enskilda fartyg.
"""

from __future__ import annotations

import json
from pathlib import Path

_MID_PATH = Path(__file__).resolve().parent.parent / "data" / "mmsi_mid.json"
_WATCH_PATH = Path(__file__).resolve().parent.parent / "data" / "shadow_watchlist.json"

# Klassiska bekvämlighetsflaggor (stora, etablerade öppna register).
FLAGS_OF_CONVENIENCE = {
    "PA", "LR", "MH", "MT", "CY", "BS", "AG", "BZ", "VC", "BM", "KY", "CW",
    "VU", "MU", "TT", "GI",
}

# Register vars TANKFLOTTOR växte kraftigt i samband med 2022 års
# sanktionsregim och som återkommande pekas ut i skuggflotte-rapportering.
SHADOW_FLEET_FLAGS = {
    "GA",  # Gabon
    "CM",  # Kamerun
    "CK",  # Cooköarna
    "KM",  # Komorerna
    "PW",  # Palau
    "BB",  # Barbados
    "ST",  # São Tomé och Príncipe
    "SZ",  # Eswatini (inlandsstat med fartygsregister)
    "GY",  # Guyana
    "DJ",  # Djibouti
    "SL",  # Sierra Leone
    "TG",  # Togo
    "TZ",  # Tanzania/Zanzibar
    "HN",  # Honduras
    "GW",  # Guinea-Bissau
    "BJ",  # Benin
    "MN",  # Mongoliet (inlandsstat med fartygsregister)
    "MD",  # Moldavien (inlandsstat med fartygsregister)
    "FM",  # Mikronesien
    "KI",  # Kiribati
}

# Global referensnivå: ungefär var tjugonde tanker seglar under ett av
# högriskregistren. Andelar klart över detta i en zon är signalen.
SHADOW_BASELINE_SHARE = 0.05
MIN_TANKERS_FOR_SIGNAL = 5

_MID: dict[str, dict] | None = None
_WATCH: dict[str, dict] | None = None


def _watchlist() -> dict[str, dict]:
    """MMSI → {imo, name} för fartyg utpekade som skuggflotta.

    1 201 fartyg ur FormerLab/shadow-fleet-tracker-light (MIT). Detta är
    NAMNGIVEN tredjepartsanalys, till skillnad från flaggheuristiken som är
    statistisk. En träff betyder att fartyget är utpekat i öppen källa —
    inte att något är rättsligt fastställt."""
    global _WATCH
    if _WATCH is None:
        try:
            _WATCH = json.loads(_WATCH_PATH.read_text(encoding="utf-8"))["by_mmsi"]
        except (OSError, ValueError, KeyError):
            _WATCH = {}
    return _WATCH


def on_watchlist(mmsi) -> dict | None:
    return _watchlist().get(str(mmsi or ""))


def _mid_table() -> dict[str, dict]:
    global _MID
    if _MID is None:
        try:
            _MID = json.loads(_MID_PATH.read_text(encoding="utf-8"))["mid"]
        except (OSError, ValueError, KeyError):
            _MID = {}
    return _MID


def flag_for(mmsi) -> dict | None:
    """MMSI → {country, iso2, foc, shadow_risk}. None för okänd/ogiltig MID."""
    s = str(mmsi or "")
    if len(s) < 9 or not s.isdigit():
        return None
    entry = _mid_table().get(s[:3])
    if not entry:
        return None
    iso2 = entry["iso2"]
    return {"country": entry["country"], "iso2": iso2,
            "foc": iso2 in FLAGS_OF_CONVENIENCE,
            "shadow_risk": iso2 in SHADOW_FLEET_FLAGS}


def enrich(items: list[dict]) -> list[dict]:
    """Sätt `flag`-blocket och ev. `watchlist`-träff på varje fartyg."""
    watch = _watchlist()
    for v in items:
        f = flag_for(v.get("mmsi"))
        if f:
            v["flag"] = f
        hit = watch.get(str(v.get("mmsi") or ""))
        if hit:
            v["watchlist"] = {"source": "shadow-fleet-tracker (MIT)",
                              "imo": hit.get("imo", ""),
                              "listed_name": hit.get("name", "")}
    return items


def shadow_exposure(items: list[dict], chokepoints: list[dict],
                    zone_of) -> dict[str, dict]:
    """Skuggflotte-exponering per sund. `zone_of(item)` → cp_id eller None."""
    buckets: dict[str, dict] = {}
    for v in items:
        cp_id = zone_of(v)
        if not cp_id:
            continue
        b = buckets.setdefault(cp_id, {"tankers": 0, "shadow": 0, "foc": 0,
                                       "flags": {}, "examples": [],
                                       "watchlisted": 0, "watch_names": []})
        if v.get("watchlist"):
            b["watchlisted"] += 1
            nm = v.get("name") or v["watchlist"].get("listed_name") or str(v.get("mmsi"))
            if len(b["watch_names"]) < 6 and nm not in b["watch_names"]:
                b["watch_names"].append(nm)
        if (v.get("category") or "") != "tanker":
            continue
        b["tankers"] += 1
        f = v.get("flag") or flag_for(v.get("mmsi"))
        if not f:
            continue
        b["flags"][f["iso2"]] = b["flags"].get(f["iso2"], 0) + 1
        if f["foc"]:
            b["foc"] += 1
        if f["shadow_risk"]:
            b["shadow"] += 1
            if len(b["examples"]) < 5:
                b["examples"].append(
                    f"{v.get('name') or v.get('mmsi')} ({f['country']})")

    out: dict[str, dict] = {}
    oil_ids = {cp["id"] for cp in chokepoints if (cp.get("oil_share_pct") or 0) > 0}
    for cp_id, b in buckets.items():
        if b["tankers"] < MIN_TANKERS_FOR_SIGNAL:
            continue
        share = b["shadow"] / b["tankers"]
        elevated = share > SHADOW_BASELINE_SHARE * 2
        entry = {
            "tankers": b["tankers"],
            "watchlisted": b["watchlisted"],
            "watch_names": b["watch_names"],
            "shadow_flagged": b["shadow"],
            "shadow_share": round(share, 2),
            "foc_share": round(b["foc"] / b["tankers"], 2),
            "top_flags": dict(sorted(b["flags"].items(),
                                     key=lambda kv: -kv[1])[:5]),
            "examples": b["examples"],
            "elevated": elevated,
            "oil_chokepoint": cp_id in oil_ids,
        }
        if b["watchlisted"]:
            entry["note"] = (
                f"{b['watchlisted']} fartyg i zonen finns på skuggflotte-"
                "bevakningslistan (1 201 namngivna fartyg kopplade till "
                "sanktionerad oljehandel, öppen tredjepartsanalys). Detta är "
                "en NAMNGIVEN träff, inte bara flaggstatistik.")
        elif elevated and entry["oil_chokepoint"]:
            entry["note"] = (
                f"{b['shadow']} av {b['tankers']} tankers i zonen ({share:.0%}) "
                "seglar under register som återkommande kopplas till "
                "sanktionsflöden — mot ~5 % i den globala tankerflottan. "
                "Konsistent med skuggflotteaktivitet vid ett oljesund. "
                "Flagg är indikation, inte bevis.")
        elif elevated:
            entry["note"] = (f"Förhöjd andel högriskflagg ({share:.0%}) men "
                             "sundet är inte ett primärt oljesund.")
        out[cp_id] = entry
    return out
