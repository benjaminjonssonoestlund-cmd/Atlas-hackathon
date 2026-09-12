"""Live-inbound per storhamn ur AIS-destinationsfältet.

Mönstret är atlas-earths analyst/shipping.py: ~var tredje fartyg i snapshotet
bär ett Destination-fält (klartext "ROTTERDAM" eller UN/LOCODE "NLRTM").
Textmatchning mot aliaslistor per hamn ger "på väg hit JUST NU" — live-
komplementet till PortWatchs dagliga anlöpshistorik (samma par som överallt
i systemet: live-AIS är nuet, PortWatch är normen).

ÄRLIGHET: matchningen är textbaserad och täcker bara fartyg som deklarerat
destination — siffran är ett GOLV, inte en totalräkning. Märks så i UI:t.
"""

from __future__ import annotations

from collections import Counter

# PortWatch-portname (nyckel matchas mot lagrets namn, case-insensitivt
# substring) → strängar som matchar AIS-destinationsfältet (klartext + LOCODE).
# Kurerad i atlas-earth; utökad med LOCODE-varianter.
PORT_DEST_ALIASES: dict[str, list[str]] = {
    "Shanghai": ["SHANGHAI", "CNSHA"],
    "Singapore": ["SINGAPORE", "SGSIN"],
    "Ningbo": ["NINGBO", "ZHOUSHAN", "CNNGB", "CNZOS"],
    "Shenzhen": ["SHENZHEN", "YANTIAN", "SHEKOU", "CNSZX", "CNYTN"],
    "Busan": ["BUSAN", "PUSAN", "KRPUS"],
    "Klang": ["PORT KLANG", "PORT KELANG", "MYPKG", "WESTPORT"],
    "Rotterdam": ["ROTTERDAM", "NLRTM"],
    "Jebel Ali": ["JEBEL ALI", "AEJEA", "DUBAI", "AEDXB"],
    "Antwerp": ["ANTWERP", "BEANR"],
    "Kaohsiung": ["KAOHSIUNG", "TWKHH"],
    "Los Angeles": ["LOS ANGELES", "USLAX"],
    "Long Beach": ["LONG BEACH", "USLGB"],
    "New York": ["NEW YORK", "USNYC", "NEWARK"],
    "Hamburg": ["HAMBURG", "DEHAM"],
    "Tanger": ["TANGER", "MAPTM"],
    "Colombo": ["COLOMBO", "LKCMB"],
    "Piraeus": ["PIRAEUS", "GRPIR"],
    "Santos": ["SANTOS", "BRSSZ"],
    "Houston": ["HOUSTON", "USHOU"],
    "Gdansk": ["GDANSK", "PLGDN"],
    "Gothenburg": ["GOTHENBURG", "GOTEBORG", "GÖTEBORG", "SEGOT"],
    "Valencia": ["VALENCIA", "ESVLC"],
    "Algeciras": ["ALGECIRAS", "ESALG"],
    "Felixstowe": ["FELIXSTOWE", "GBFXT"],
    "Le Havre": ["LE HAVRE", "FRLEH"],
}


def inbound_by_alias(items: list[dict]) -> dict[str, dict]:
    """{aliasnyckel: {inbound, by_category, next_dests_exempel}} ur snapshotet."""
    out: dict[str, dict] = {}
    hits: dict[str, list[dict]] = {k: [] for k in PORT_DEST_ALIASES}
    for v in items:
        dest = (v.get("dest") or "").upper()
        if not dest:
            continue
        for key, aliases in PORT_DEST_ALIASES.items():
            if any(a in dest for a in aliases):
                hits[key].append(v)
                break
    for key, ships in hits.items():
        if not ships:
            continue
        cats = Counter((s.get("category") or "okänt") for s in ships)
        moving = sum(1 for s in ships if (s.get("sog") or 0) >= 0.5)
        out[key] = {
            "inbound": len(ships),
            "underway": moving,
            "by_category": dict(cats.most_common(3)),
        }
    return out


def attach_inbound(layer_rows: list[dict], inbound: dict[str, dict]) -> None:
    """Sätt `inbound`-blocket på lagerrader vars namn matchar en aliasnyckel."""
    for row in layer_rows:
        name = (row.get("name") or "").lower()
        for key, data in inbound.items():
            if key.lower() in name:
                row["live_inbound"] = data
                break
