"""Ruttabell och segmentklassning (domestic / ACMI / charter).

Klassreglerna är ANVÄNDARENS beslut, inte kodens: route_table.csv visar alla
rutter med volym och ett FÖRSLAG, och det som räknas är det som står i
manual/route_classes.csv. Ofyllda rutter hamnar i "unclassified" och syns
i kvartalssummeringen i stället för att tyst läggas i något segment.

Förslagen följer specen: domestic = inrikes Kanada; ACMI = CVG/MIA söderut
(DHL-nätet); charter = transpacifiskt, Liège–Tel Aviv. Allt annat (transborder
Kanada–USA, transatlantiskt från Kanada, US-inrikes, Bermuda, positionering)
föreslås som "?" och väntar på beslut.

manual/route_classes.csv — en regel per rad, första träff vinner, ordnat efter
specificitet (flygplats > land > kontinent > "*"):
    dep,arr,segment,note
    YHM,YVR,domestic,
    cc:CA,cc:CA,domestic,inrikes Kanada oavsett tid
    CVG,cont:SA,acmi,DHL söderut
Regler gäller i båda riktningarna om inte `directional` sätts till 1.
"""

from __future__ import annotations

import pandas as pd

from .common import MANUAL, PROCESSED

RULES_CSV = MANUAL / "route_classes.csv"
ROUTE_TABLE = PROCESSED / "route_table.csv"
QUARTERLY = PROCESSED / "adsb_quarterly_segments.csv"
RULE_COLUMNS = ["dep", "arr", "segment", "directional", "note"]
SEGMENTS = ("domestic", "acmi", "charter", "ferry", "exclude")

DHL_SOUTH_HUBS = {"CVG", "MIA"}
NIGHT_HOURS_ET = set(range(19, 24)) | set(range(0, 7))


def _latam(country: str, continent: str) -> bool:
    return continent == "SA" or (continent == "NA" and country not in ("CA", "US"))


def suggest(r) -> str:
    dc, ac = r["dep_country"], r["arr_country"]
    dk, ak = r["dep_continent"], r["arr_continent"]
    if dc == "CA" and ac == "CA":
        return "domestic"
    south = ((r["dep"] in DHL_SOUTH_HUBS and _latam(ac, ak))
             or (r["arr"] in DHL_SOUTH_HUBS and _latam(dc, dk))
             or (_latam(dc, dk) and _latam(ac, ak)))
    if south:
        return "acmi"
    if {dk, ak} & {"AS", "OC"} and {dk, ak} & {"NA", "EU"}:
        return "charter"
    if {r["dep"], r["arr"]} == {"LGG", "TLV"}:
        return "charter"
    return "?"


def _matches(pattern: str, code: str, country: str, continent: str) -> bool:
    if pattern == "*":
        return True
    if pattern == "latam":   # "söderut": Sydamerika + Nord-/Centralamerika utom CA/US (inkl. Karibien, PR)
        return _latam(country, continent)
    if pattern.startswith("cc:"):
        return pattern[3:] == country
    if pattern.startswith("cont:"):
        return pattern[5:] == continent
    return pattern == code


def _specificity(pattern: str) -> int:
    if pattern == "*":
        return 0
    if pattern == "latam" or pattern.startswith("cont:"):
        return 1
    return 2 if pattern.startswith("cc:") else 3


def load_rules() -> pd.DataFrame:
    if not RULES_CSV.exists():
        RULES_CSV.parent.mkdir(parents=True, exist_ok=True)
        RULES_CSV.write_text(",".join(RULE_COLUMNS) + "\n", encoding="utf-8")
    rules = pd.read_csv(RULES_CSV, dtype=str, keep_default_na=False)
    bad = set(rules["segment"]) - set(SEGMENTS)
    if bad:
        raise ValueError(f"okända segment i {RULES_CSV.name}: {bad} (tillåtna: {SEGMENTS})")
    rules["_spec"] = rules["dep"].map(_specificity) + rules["arr"].map(_specificity)
    return rules.sort_values("_spec", ascending=False, kind="stable")


def classify(r, rules: pd.DataFrame) -> str:
    for rule in rules.itertuples():
        for a, b in ((("dep", "arr"),) if rule.directional == "1" else (("dep", "arr"), ("arr", "dep"))):
            if (_matches(rule.dep, r[a], r[f"{a}_country"], r[f"{a}_continent"])
                    and _matches(rule.arr, r[b], r[f"{b}_country"], r[f"{b}_continent"])):
                return rule.segment
    return "unclassified"


def usable(flights: pd.DataFrame) -> pd.DataFrame:
    return flights[~flights["glitch"].astype(bool)]


def route_table(flights: pd.DataFrame) -> pd.DataFrame:
    f = usable(flights).copy()
    f["night"] = f["dep_local_hour_et"].isin(NIGHT_HOURS_ET)
    f["inferred"] = f["quality"].eq("inferred")
    keys = ["dep", "arr", "dep_country", "arr_country", "dep_continent", "arr_continent"]
    g = f.groupby(keys, dropna=False)
    t = g.agg(flights=("block_h", "size"), cycles=("cycles", "sum"), block_h=("block_h", "sum"),
              median_block_h=("block_h", "median"), gc_km=("gc_km", "median"),
              night_share=("night", "mean"), inferred_share=("inferred", "mean"),
              suspect_share=("suspect", "mean"),
              first_seen=("off_block_utc", "min"), last_seen=("off_block_utc", "max"),
              callsigns=("callsign", lambda s: " ".join(sorted({c for c in s if isinstance(c, str) and c})[:5])),
              aircraft=("registration", "nunique")).reset_index()
    t["suggested"] = t.apply(suggest, axis=1)
    rules = load_rules()
    t["segment"] = t.apply(lambda r: classify(r, rules), axis=1)
    t = t.sort_values("block_h", ascending=False)
    for col in ("block_h", "median_block_h"):
        t[col] = t[col].round(2)
    for col in ("night_share", "inferred_share", "suspect_share"):
        t[col] = t[col].round(3)
    ROUTE_TABLE.parent.mkdir(parents=True, exist_ok=True)
    t.to_csv(ROUTE_TABLE, index=False)
    return t


def with_segments(flights: pd.DataFrame) -> pd.DataFrame:
    """Användbara flygningar + kolumnen `segment` enligt manual/route_classes.csv."""
    f = usable(flights).copy()
    rules = load_rules()
    keys = ["dep", "arr", "dep_country", "arr_country", "dep_continent", "arr_continent"]
    route_seg = f[keys].drop_duplicates()
    route_seg["segment"] = route_seg.apply(lambda r: classify(r, rules), axis=1)
    return f.merge(route_seg, on=keys, how="left")


def quarterly_segments(flights: pd.DataFrame) -> pd.DataFrame:
    """Blocktimmar och cykler per kvartal × segment, med andel skattad tid."""
    f = with_segments(flights)
    f["full"] = f["quality"].eq("full")
    f["inferred"] = f["quality"].eq("inferred")
    out = f.groupby(["quarter", "segment"]).agg(
        block_h=("block_h", "sum"), cycles=("cycles", "sum"),
        full_share=("full", "mean"), inferred_block_h=("block_h", lambda s: s[f.loc[s.index, "inferred"]].sum()),
        aircraft=("registration", "nunique")).reset_index()
    out.to_csv(QUARTERLY, index=False)
    return out
