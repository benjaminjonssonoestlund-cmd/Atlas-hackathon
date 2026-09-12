"""Flygfrakt som substitutionssignal för störd sjöfart.

Tesen (etablerad i fraktekonomi): när ett sund stryps flyttar högvärdesgods
från containerfartyg till lastflyg. Flygfraktrater spikade mätbart under både
Röda havet-krisen 2024 och Ever Given 2021. Fraktflyg nära ett stört sund är
därför (a) en OBEROENDE bekräftelse att störningen biter i realekonomin, och
(b) en ledande indikator för flygfraktpriser och logistikbolagens marginaler.

Signalen är medvetet försiktig: den påstår inte kausalitet, den visar
samvariation — "sundet är stört OCH fraktflyget över zonen är förhöjt".
Utan egen historisk baslinje (som byggs upp av live.record) rapporteras
råa tal, inte avvikelser. Det märks i UI:t.
"""

from __future__ import annotations

# Andel fraktflyg av all trafik i zonen som räknas som "förhöjt".
# Globalt är ~2-4 % av flygrörelserna ren frakt; över ett stört sund kan
# andelen mångdubblas när gods byter transportslag.
CARGO_SHARE_ELEVATED = 0.12
MIN_FLIGHTS_FOR_SHARE = 8


def by_chokepoint(flights: list[dict], chokepoints: list[dict],
                  stress_by_id: dict[str, dict | None]) -> dict[str, dict]:
    """Flygläget per sund + substitutionsflagga när sundet samtidigt är stört."""
    out: dict[str, dict] = {}
    for cp in chokepoints:
        near = [f for f in flights if f.get("near") == cp["id"]]
        if not near:
            continue
        cargo = [f for f in near if f.get("cargo")]
        share = len(cargo) / len(near) if near else 0.0
        stress = stress_by_id.get(cp["id"]) or {}
        disrupted = bool(stress.get("ongoing_since"))
        elevated = (len(near) >= MIN_FLIGHTS_FOR_SHARE
                    and share >= CARGO_SHARE_ELEVATED)
        entry = {
            "flights": len(near),
            "cargo": len(cargo),
            "cargo_share": round(share, 2),
            "elevated": elevated,
            "substitution": disrupted and elevated,
            "top_cargo": [f["flight"] for f in cargo[:5]],
        }
        if entry["substitution"]:
            entry["note"] = (
                f"Sjöfarten är störd OCH {len(cargo)} av {len(near)} flyg i zonen "
                f"({share:.0%}) är fraktflyg — konsistent med att högvärdesgods "
                "flyttar från sjö till luft. Flygfraktrater och logistikbolag "
                "(FDX, UPS, ATSG) är exponerade.")
        elif disrupted:
            entry["note"] = ("Sjöfarten är störd men fraktflyget över zonen är "
                             "på normal nivå — ingen tydlig substitution ännu.")
        out[cp["id"]] = entry
    return out


MIL_NEAR_KM = 400.0


def military_near(mil: list[dict], chokepoints: list[dict]) -> dict[str, dict]:
    """Militärflyg inom 400 km av varje sund — eskaleringsindikator.

    Marinspaning, tankflyg och transportflyg koncentreras där kriser byggs
    upp, ofta innan något rapporteras officiellt. Rå observation, ingen
    tolkning av avsikt."""
    from .live import haversine_km
    out: dict[str, dict] = {}
    for cp in chokepoints:
        near = []
        for a in mil:
            if abs(a["lat"] - cp["lat"]) > 5.0 or abs(a["lon"] - cp["lon"]) > 6.0:
                continue
            km = haversine_km(cp["lat"], cp["lon"], a["lat"], a["lon"])
            if km <= MIL_NEAR_KM:
                near.append({**a, "km": round(km)})
        if near:
            near.sort(key=lambda a: a["km"])
            out[cp["id"]] = {
                "count": len(near),
                "closest_km": near[0]["km"],
                "aircraft": [{"flight": a["flight"], "type": a["type"],
                              "km": a["km"]} for a in near[:6]],
            }
    return out


def totals(flights: list[dict]) -> dict:
    cargo = sum(1 for f in flights if f.get("cargo"))
    return {"flights": len(flights), "cargo": cargo,
            "cargo_share": round(cargo / len(flights), 3) if flights else None}
