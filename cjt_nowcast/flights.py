"""Flygningar och blocktimmar ur råa ADS-B-spår.

Blocktimme (Cargojets definition i MD&A): från att bromsarna släpps vid
uppställningsplatsen till att de sätts efter landning. Vi mäter det som
"sista stillastående → första stillastående" på marken, och skattar det som
inte syns.

VARFÖR SÅ HÄR (uppmätt på riktiga Cargojet-spår 2024–2026):
- readsb:s ben-flagga (flags & 2) sätts nästan aldrig → vi segmenterar själva.
- Marktäckningen varierar kraftigt: YYC/YXE syns fint, navet YHM saknar ofta
  markpunkter helt. Taxitid måste därför skattas per flygplats där den syns
  och fyllas i där den inte gör det.
- Luckor på 20–240 min mitt i flygningar är vanliga.

Metod:
1. Punkter klassas: MARKKONTAKT (markpunkt ≤5 km från flygplats, eller låg
   luftpunkt ≤25 km och <4 000 ft över fältet), HÖG (övrigt luftburet) eller
   okänd.
2. BESÖK = maximal följd av kontaktpunkter vid samma flygplats utan höga
   punkter emellan. Ett besök kan sakna landning eller start (täckningshål).
3. FLYGNING = två på varandra följande besök. Syns varken start eller landning
   och inga luftpunkter finns emellan, men planet dyker upp på en ANNAN
   flygplats, så måste en flygning ha skett → läggs in som "inferred" med
   distansmodell. (Osedda tur-och-retur-rotationer går inte att upptäcka;
   det är den kvarvarande systematiska underskattningen.)
4. Fyllnadspass över hela flottan: luftburen tid ~ a + b·storcirkelavstånd
   skattas på fullt observerade flygningar; taxi ut/in skattas som median per
   flygplats (≥8 observationer, annars global median).

Varje flygning bär kvalitetsflaggor så att valideringen kan visa hur mycket av
kvartalets blocktimmar som är mätta respektive skattade.
"""

from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree

from . import adsb, airports
from .common import PROCESSED, quarter_label

LOG = logging.getLogger("cjt.flights")

ET = ZoneInfo("America/Toronto")   # Cargojets rapportkvartal: östkusttid
FLIGHTS_CSV = PROCESSED / "flights.csv.gz"
EARTH_KM = 6371.0
KT_TO_KMH = 1.852


@dataclass(frozen=True)
class Params:
    ground_airport_km: float = 5.0
    low_airport_km: float = 25.0
    low_agl_ft: float = 4000.0
    candidate_alt_ft: float = 14000.0   # över detta aldrig flygplatskontakt (Mexico City ligger på 7 300 ft)
    run_split_km: float = 40.0
    link_gap_s: float = 300.0           # mark↔luft inom detta = observerad start/landning
    ground_continuity_s: float = 300.0  # längre lucka bryter en taxikedja
    parked_min_s: float = 300.0         # stillastående så länge = uppställd (inte väntan vid bana)
    parked_gs_kt: float = 1.5
    parked_lost_s: float = 600.0        # stilla + täckningen försvinner = uppställd
    descent_fpm: float = 800.0
    climb_fpm: float = 2000.0
    vrate_fpm: float = 200.0
    transit_kt: float = 280.0           # medelfart mellan fält och första/sista luftpunkt
    long_gap_h: float = 18.0
    taxi_min_obs: int = 8
    # Besök utan markpunkter måste gå så här lågt — annars överflygning. Uppmätt:
    # inflygning till YHM passerar YKF på ~2 500 ft och gav ett falskt ben.
    visit_min_agl_ft: float = 1500.0
    # Samma fält, längre bort än så och längre i luften → osedd mellanlandning
    roundtrip_min_km: float = 150.0
    roundtrip_min_airborne_h: float = 1.0
    turnaround_default_h: float = 2.0


P = Params()


# ---------- Punkter ----------

@dataclass
class Points:
    ts: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    alt: np.ndarray      # ft baro, NaN om okänd eller mark
    ground: np.ndarray   # bool
    gs: np.ndarray       # kt, NaN om okänd
    rate: np.ndarray     # ft/min, NaN om okänd
    callsign: np.ndarray  # object, "" om okänd

    def __len__(self) -> int:
        return len(self.ts)


def points_from_rows(rows: list[tuple]) -> Points:
    """rows: (ts, lat, lon, alt|'ground'|None, gs, rate, callsign)."""
    rows = sorted(rows, key=lambda r: r[0])
    # dubbletter uppstår i dagsskarvar (spåret för dag D+1 kan börja med D:s sista punkt)
    dedup, last = [], None
    for r in rows:
        if r[0] != last:
            dedup.append(r)
            last = r[0]
    n = len(dedup)
    ts = np.fromiter((r[0] for r in dedup), float, n)
    alt_raw = [r[3] for r in dedup]
    ground = np.fromiter((a == "ground" for a in alt_raw), bool, n)
    alt = np.fromiter((a if isinstance(a, (int, float)) else np.nan for a in alt_raw), float, n)
    num = lambda i: np.fromiter(  # noqa: E731
        (r[i] if isinstance(r[i], (int, float)) else np.nan for r in dedup), float, n)
    return Points(ts, num(1), num(2), alt, ground, num(4), num(5),
                  np.array([r[6] for r in dedup], dtype=object))


def load_points(hexid: str, days: list[date]) -> Points:
    rows: list[tuple] = []
    for d in days:
        j = adsb.load_trace(hexid, d)
        if not j:
            continue
        t0 = float(j["timestamp"])
        for p in j["trace"]:
            det = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
            cs = (det.get("flight") or "").strip() if det else ""
            rows.append((t0 + p[0], p[1], p[2], p[3], p[4], p[7] if len(p) > 7 else None, cs))
    return points_from_rows(rows)


# ---------- Geometri ----------

def haversine_km(lat1, lon1, lat2, lon2):
    la1, lo1, la2, lo2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((la2 - la1) / 2) ** 2 + np.cos(la1) * np.cos(la2) * np.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


SHADOW_KM = 6.0


def _priority(df: pd.DataFrame) -> np.ndarray:
    iata = df["iata"].astype(bool) if "iata" in df else df["code"].str.fullmatch(r"[A-Z0-9]{3}")
    large = df["type"].eq("large_airport")
    # 0 stor+IATA, 1 medel+IATA, 2 stor utan IATA, 3 övrigt — linjefält före småfält
    return np.where(iata, 0, 2) + np.where(large, 0, 1)


class AirportLookup:
    def __init__(self, df: pd.DataFrame):
        df = df.reset_index(drop=True)
        # Fält i skuggan av ett högre prioriterat fält (≤6 km) tas bort helt: annars
        # vinner de "närmast"-matchningen för markpunkter på den stora flygplatsen
        # och kan dela ett markbesök i två (uppmätt: YEG-markpunkter → "CA-1291").
        prio = _priority(df)
        tree = BallTree(np.radians(df[["lat", "lon"]].to_numpy(float)), metric="haversine")
        neigh = tree.query_radius(np.radians(df[["lat", "lon"]].to_numpy(float)), r=SHADOW_KM / EARTH_KM)
        shadowed = np.array([bool(len(nb)) and prio[nb].min() < prio[i] for i, nb in enumerate(neigh)])
        self.df = df[~shadowed].reset_index(drop=True)
        self.lat = self.df["lat"].to_numpy(float)
        self.lon = self.df["lon"].to_numpy(float)
        self.elev = self.df["elev_ft"].to_numpy(float)
        self.tree = BallTree(np.radians(np.c_[self.lat, self.lon]), metric="haversine")
        self.priority = _priority(self.df)

    def nearest(self, lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if len(lat) == 0:
            return np.array([], int), np.array([], float)
        dist, idx = self.tree.query(np.radians(np.c_[lat, lon]), k=1)
        return idx[:, 0], dist[:, 0] * EARTH_KM

    def best_near(self, lat: float, lon: float, radius_km: float) -> int | None:
        """Linjefält före småfält inom radien; närmast inom samma prioritet.

        Utan detta hamnade inflygningar mot YEG/YYC på närliggande fält utan
        IATA-kod (uppmätt: "CA-1291")."""
        idx, dist = self.tree.query_radius(np.radians([[lat, lon]]), r=radius_km / EARTH_KM,
                                           return_distance=True)
        idx, dist = idx[0], dist[0]
        if len(idx) == 0:
            return None
        return int(idx[np.lexsort((dist, self.priority[idx]))[0]])


@lru_cache(maxsize=1)
def default_lookup() -> AirportLookup:
    return AirportLookup(airports.load())


# ---------- Segmentering ----------

def _mode(values) -> int:
    return Counter(values.tolist()).most_common(1)[0][0]


def _stationary(gs: float, prm: Params) -> bool:
    return not np.isnan(gs) and gs <= prm.parked_gs_kt


def _on_block(pts: Points, G: np.ndarray, touchdown: float, prm: Params) -> float | None:
    """Första uppställning efter landning, om taxikedjan är obruten dit."""
    after = G[pts.ts[G] >= touchdown - 1]
    k = 0
    while k < len(after):
        i = after[k]
        if k > 0 and pts.ts[i] - pts.ts[after[k - 1]] > prm.ground_continuity_s:
            return None                           # täckningen bröts medan planet rörde sig
        if _stationary(pts.gs[i], prm):
            j = k
            while j + 1 < len(after) and _stationary(pts.gs[after[j + 1]], prm) \
                    and pts.ts[after[j + 1]] - pts.ts[after[j]] <= prm.ground_continuity_s:
                j += 1
            span = pts.ts[after[j]] - pts.ts[i]
            lost = (j + 1 == len(after)
                    or pts.ts[after[j + 1]] - pts.ts[after[j]] > prm.parked_lost_s)
            if span >= prm.parked_min_s or lost:
                return float(pts.ts[i])
            k = j + 1
            continue
        k += 1
    return None


def _off_block(pts: Points, G: np.ndarray, liftoff: float, prm: Params) -> float | None:
    """Sista uppställning före start (bakåt från starten), om kedjan är obruten."""
    before = G[pts.ts[G] <= liftoff + 1][::-1]
    k = 0
    while k < len(before):
        i = before[k]
        if k > 0 and pts.ts[before[k - 1]] - pts.ts[i] > prm.ground_continuity_s:
            return None
        if _stationary(pts.gs[i], prm):
            j = k
            while j + 1 < len(before) and _stationary(pts.gs[before[j + 1]], prm) \
                    and pts.ts[before[j]] - pts.ts[before[j + 1]] <= prm.ground_continuity_s:
                j += 1
            span = pts.ts[i] - pts.ts[before[j]]
            lost = (j + 1 == len(before)
                    or pts.ts[before[j]] - pts.ts[before[j + 1]] > prm.parked_lost_s)
            if span >= prm.parked_min_s or lost:
                return float(pts.ts[i])           # sista stillastående punkt före rörelse
            k = j + 1
            continue
        k += 1
    return None


def _vertical(pts: Points, i: int, prm: Params) -> int:
    """+1 stigning, −1 sjunkande, 0 okänt — baro_rate, annars höjdtrend mot nästa punkt."""
    r = pts.rate[i]
    if np.isnan(r) and i + 1 < len(pts) and not np.isnan(pts.alt[i + 1]) and not np.isnan(pts.alt[i]):
        dt = pts.ts[i + 1] - pts.ts[i]
        if 0 < dt <= 120:
            r = (pts.alt[i + 1] - pts.alt[i]) / dt * 60
    if np.isnan(r):
        return 0
    return 1 if r > prm.vrate_fpm else (-1 if r < -prm.vrate_fpm else 0)


def find_visits(pts: Points, lookup: AirportLookup, prm: Params = P) -> tuple[list[dict], np.ndarray]:
    n = len(pts)
    if n == 0:
        return [], np.zeros(0, bool)
    alt_f = np.where(np.isnan(pts.alt), np.inf, pts.alt)
    cand = pts.ground | (alt_f < prm.candidate_alt_ft)
    ap_idx = np.full(n, -1)
    ap_km = np.full(n, np.inf)
    idx, km = lookup.nearest(pts.lat[cand], pts.lon[cand])
    ap_idx[cand], ap_km[cand] = idx, km
    elev = np.where(ap_idx >= 0, lookup.elev[np.maximum(ap_idx, 0)], 0.0)
    agl = pts.alt - elev

    ground_c = pts.ground & (ap_km <= prm.ground_airport_km)
    low_c = ~pts.ground & (ap_km <= prm.low_airport_km) & (agl < prm.low_agl_ft)
    contact = ground_c | low_c
    high_raw = ~pts.ground & ~low_c & (np.isfinite(pts.alt) | (np.nan_to_num(pts.gs) > 150))
    # en enstaka felaktig höjd nära fältet får inte dela ett besök i två (fantomflygning)
    high = high_raw & (np.r_[False, high_raw[:-1]] | np.r_[high_raw[1:], False])

    def _groups(contact: np.ndarray, high: np.ndarray) -> list[np.ndarray]:
        ci = np.flatnonzero(contact)
        if len(ci) == 0:
            return []
        hc = np.cumsum(high)
        a, b = ci[:-1], ci[1:]
        split = ((hc[b - 1] - hc[a]) > 0) \
            | (haversine_km(pts.lat[a], pts.lon[a], pts.lat[b], pts.lon[b]) > prm.run_split_km) \
            | (ground_c[a] & ground_c[b] & (ap_idx[a] != ap_idx[b]))
        return np.split(ci, np.flatnonzero(split) + 1)

    groups = _groups(contact, high)
    # Besök utan markpunkter som aldrig gick riktigt lågt är överflygningar:
    # degradera deras punkter till luftburna och gruppera om
    overflight = np.zeros(n, bool)
    for g in groups:
        if not ground_c[g].any() and np.nanmin(agl[g]) >= prm.visit_min_agl_ft:
            overflight[g] = True
    if overflight.any():
        low_c = low_c & ~overflight
        contact = ground_c | low_c
        high = high | overflight
        groups = _groups(contact, high)
    if not groups:
        return [], high

    visits = []
    for g in groups:
        G = g[ground_c[g]]
        L = g[low_c[g]]
        if len(G):
            airport = _mode(ap_idx[G])
        else:
            lowest = g[np.nanargmin(agl[g])]
            best = lookup.best_near(pts.lat[lowest], pts.lon[lowest], prm.low_airport_km)
            airport = best if best is not None else int(ap_idx[lowest])
        v = {"airport": airport, "first_i": int(g[0]), "last_i": int(g[-1]),
             "first_ts": float(pts.ts[g[0]]), "last_ts": float(pts.ts[g[-1]]),
             "first_ground_ts": float(pts.ts[G[0]]) if len(G) else None,
             "last_ground_ts": float(pts.ts[G[-1]]) if len(G) else None,
             "n_ground": int(len(G)),
             "max_gap_h": float(np.diff(pts.ts[g]).max() / 3600) if len(g) > 1 else 0.0,
             "touchdown": None, "td_q": None, "liftoff": None, "lo_q": None,
             "on_block": None, "off_block": None}
        vert = {int(i): _vertical(pts, int(i), prm) for i in L}
        desc = [i for i in L if vert[int(i)] < 0]
        climb = [i for i in L if vert[int(i)] > 0]

        # Landning
        if len(G):
            g0 = G[0]
            prev = g0 - 1
            if prev >= 0 and not pts.ground[prev] and pts.ts[g0] - pts.ts[prev] <= prm.link_gap_s:
                v["touchdown"], v["td_q"] = float((pts.ts[g0] + pts.ts[prev]) / 2), "obs"
            else:
                pre = [i for i in desc if i < g0]
                if pre:
                    i = pre[-1]
                    v["touchdown"] = float(min(pts.ts[g0], pts.ts[i] + max(agl[i], 0) / prm.descent_fpm * 60))
                    v["td_q"] = "est"
        elif desc:
            first_climb_after = [c for c in climb if c > desc[0]]
            last_desc = max([d for d in desc if not first_climb_after or d < first_climb_after[0]] or [desc[-1]])
            v["touchdown"] = float(pts.ts[last_desc] + max(agl[last_desc], 0) / prm.descent_fpm * 60)
            v["td_q"] = "est"

        # Start
        if len(G):
            gn = G[-1]
            nxt = gn + 1
            if nxt < n and not pts.ground[nxt] and pts.ts[nxt] - pts.ts[gn] <= prm.link_gap_s:
                v["liftoff"], v["lo_q"] = float((pts.ts[gn] + pts.ts[nxt]) / 2), "obs"
            else:
                post = [i for i in climb if i > gn]
                if post:
                    i = post[0]
                    v["liftoff"] = float(max(pts.ts[gn], pts.ts[i] - max(agl[i], 0) / prm.climb_fpm * 60))
                    v["lo_q"] = "est"
        elif climb:
            after_td = [c for c in climb if v["touchdown"] is None or pts.ts[c] >= v["touchdown"]]
            if after_td:
                i = after_td[0]
                v["liftoff"] = float(pts.ts[i] - max(agl[i], 0) / prm.climb_fpm * 60)
                v["lo_q"] = "est"

        if len(G) and v["touchdown"] is not None:
            v["on_block"] = _on_block(pts, G, v["touchdown"], prm)
        if len(G) and v["liftoff"] is not None:
            v["off_block"] = _off_block(pts, G, v["liftoff"], prm)
        visits.append(v)
    return visits, high


def segment(pts: Points, lookup: AirportLookup, hexid: str = "", prm: Params = P) -> list[dict]:
    """Flygningar mellan på varandra följande besök (tider i epoch-sekunder, NaN = okänt)."""
    visits, high = find_visits(pts, lookup, prm)
    out = []
    for v0, v1 in zip(visits, visits[1:]):
        lo, hi = v0["last_i"] + 1, v1["first_i"]
        air = np.flatnonzero(high[lo:hi]) + lo
        dep, arr = v0["airport"], v1["airport"]
        if dep == arr and len(air) == 0:
            continue  # kan inte hända efter besöksbygget, men skydda mot framtida ändringar
        dlat, dlon = lookup.lat[dep], lookup.lon[dep]
        alat, alon = lookup.lat[arr], lookup.lon[arr]
        gc_km = float(haversine_km(dlat, dlon, alat, alon))
        speed_kms = prm.transit_kt * KT_TO_KMH / 3600

        takeoff, to_q = v0["liftoff"], v0["lo_q"]
        if takeoff is None and len(air):
            t = pts.ts[air[0]] - float(haversine_km(dlat, dlon, pts.lat[air[0]], pts.lon[air[0]])) / speed_kms
            takeoff, to_q = (max(t, v0["last_ground_ts"]) if v0["last_ground_ts"] else t), "air"
        landing, ld_q = v1["touchdown"], v1["td_q"]
        if landing is None and len(air):
            t = pts.ts[air[-1]] + float(haversine_km(pts.lat[air[-1]], pts.lon[air[-1]], alat, alon)) / speed_kms
            landing, ld_q = (min(t, v1["first_ground_ts"]) if v1["first_ground_ts"] else t), "air"

        cs = [c for c in pts.callsign[v0["last_i"]:v1["first_i"] + 1] if c]
        out.append({
            "hex": hexid,
            "dep": lookup.df.at[dep, "code"], "arr": lookup.df.at[arr, "code"],
            "dep_country": lookup.df.at[dep, "country"], "arr_country": lookup.df.at[arr, "country"],
            "dep_continent": lookup.df.at[dep, "continent"], "arr_continent": lookup.df.at[arr, "continent"],
            "gc_km": round(gc_km, 1),
            "callsign": Counter(cs).most_common(1)[0][0] if cs else "",
            "off_block": v0["off_block"], "takeoff": takeoff, "landing": landing,
            "on_block": v1["on_block"],
            "to_q": to_q, "ld_q": ld_q,
            "dep_last_contact": v0["last_ts"], "arr_first_contact": v1["first_ts"],
            "n_air_points": int(len(air)),
            "max_km_from_dep": float(haversine_km(dlat, dlon, pts.lat[air], pts.lon[air]).max()) if len(air) else 0.0,
            "air_first_ts": float(pts.ts[air[0]]) if len(air) else None,
            "air_last_ts": float(pts.ts[air[-1]]) if len(air) else None,
            "max_air_gap_min": float(np.diff(pts.ts[lo - 1:hi + 1]).max() / 60) if hi > lo else 0.0,
            "dep_visit_max_gap_h": v0["max_gap_h"],
        })
    return out


# ---------- Fyllnad över hela flottan ----------

def _fit_airborne(df: pd.DataFrame) -> tuple[float, float]:
    """Luftburen tid [min] ≈ a + b·km på flygningar med observerad/skattad start OCH landning."""
    ok = df["to_q"].isin(["obs", "est"]) & df["ld_q"].isin(["obs", "est"]) & (df["gc_km"] > 150)
    x = df.loc[ok, "gc_km"].to_numpy(float)
    y = ((df.loc[ok, "landing"] - df.loc[ok, "takeoff"]) / 60).to_numpy(float)
    keep = (y > 10) & (y < 20 * 60)
    x, y = x[keep], y[keep]
    if len(x) < 20:
        return 20.0, 60 / 780  # ~780 km/h + 20 min klättring/inflygning
    for _ in range(3):  # trimma grova avvikare (felmatchade flygplatser, holding)
        b, a = np.polyfit(x, y, 1)
        res = y - (a + b * x)
        mad = np.median(np.abs(res - np.median(res))) or 1.0
        m = np.abs(res) < 4 * 1.4826 * mad
        x, y = x[m], y[m]
    b, a = np.polyfit(x, y, 1)
    return float(a), float(b)


def fill(df: pd.DataFrame, prm: Params = P) -> tuple[pd.DataFrame, dict]:
    df = df.copy()
    for col in ("off_block", "takeoff", "landing", "on_block", "air_first_ts", "air_last_ts"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    a, b = _fit_airborne(df)
    model_s = (a + b * df["gc_km"]) * 60

    # Start/landning som bara syns i ena änden
    m = df["takeoff"].isna() & df["landing"].notna()
    df.loc[m, "takeoff"] = df.loc[m, "landing"] - model_s[m]
    df.loc[m, "to_q"] = "model"
    m = df["landing"].isna() & df["takeoff"].notna()
    df.loc[m, "landing"] = df.loc[m, "takeoff"] + model_s[m]
    df.loc[m, "ld_q"] = "model"
    # Ingen av ändarna — flygningen är härledd ur att planet bytt flygplats
    m = df["takeoff"].isna() & df["landing"].isna()
    mid = np.where(df["air_first_ts"].notna(), (df["air_first_ts"] + df["air_last_ts"]) / 2,
                   (df["dep_last_contact"] + df["arr_first_contact"]) / 2)
    df.loc[m, "takeoff"] = mid[m] - model_s[m] / 2
    df.loc[m, "landing"] = mid[m] + model_s[m] / 2
    df.loc[m, ["to_q", "ld_q"]] = "inferred"

    # Taxitider per flygplats ur fullt observerade markkedjor
    taxi_out = (df["takeoff"] - df["off_block"]) / 60
    taxi_in = (df["on_block"] - df["landing"]) / 60
    ok_out = df["off_block"].notna() & (df["to_q"] == "obs") & taxi_out.between(2, 60)
    ok_in = df["on_block"].notna() & (df["ld_q"] == "obs") & taxi_in.between(1, 60)
    g_out = float(taxi_out[ok_out].median()) if ok_out.any() else 12.0
    g_in = float(taxi_in[ok_in].median()) if ok_in.any() else 7.0
    per_out = taxi_out[ok_out].groupby(df.loc[ok_out, "dep"]).agg(["median", "size"])
    per_in = taxi_in[ok_in].groupby(df.loc[ok_in, "arr"]).agg(["median", "size"])
    med_out = per_out.loc[per_out["size"] >= prm.taxi_min_obs, "median"]
    med_in = per_in.loc[per_in["size"] >= prm.taxi_min_obs, "median"]
    est_out = df["dep"].map(med_out).fillna(g_out) * 60
    est_in = df["arr"].map(med_in).fillna(g_in) * 60

    df["off_q"] = np.where(ok_out, "obs", "taxi_est")
    df["on_q"] = np.where(ok_in, "obs", "taxi_est")
    df.loc[~ok_out, "off_block"] = df.loc[~ok_out, "takeoff"] - est_out[~ok_out]
    df.loc[~ok_in, "on_block"] = df.loc[~ok_in, "landing"] + est_in[~ok_in]

    df["airborne_h"] = (df["landing"] - df["takeoff"]) / 3600
    df["block_h"] = (df["on_block"] - df["off_block"]) / 3600
    df["model_airborne_h"] = model_s / 3600
    ratio = df["airborne_h"] / df["model_airborne_h"].clip(lower=0.2)
    df["suspect"] = (ratio < 0.5) | (ratio > 2.0) | (df["block_h"] <= 0) | (df["block_h"] > 20)
    # Samma fält, kort luftfärd: fantom ur täckningsbrus, inte en provflygning
    df["glitch"] = (df["dep"] == df["arr"]) & (df["airborne_h"] < 0.25)
    df["cycles"] = 1

    # Samma fält men långt bort och länge: tur och retur med osedd mellanlandning
    # (uppmätt: MIA→MIA 7–16 h). Två ben; blocktiden minus vändtid på utestationen.
    turnaround_h = _median_turnaround(df, prm)
    rt = ((df["dep"] == df["arr"]) & (df["max_km_from_dep"] > prm.roundtrip_min_km)
          & (df["airborne_h"] > prm.roundtrip_min_airborne_h))
    span_based = np.maximum(df.loc[rt, "block_h"] - turnaround_h, df.loc[rt, "block_h"] * 0.5)
    # Tak: en tur och retur till längsta observerade punkten (distansmodell + taxi i båda ändar).
    # Utan taket räknades flerstoppsrotationer och långa markuppehåll ute som blocktid —
    # uppmätt: +73 % mot rapporterat för 2025Q2 när veckor syddes ihop.
    one_leg_h = (a + b * df.loc[rt, "max_km_from_dep"]) / 60
    distance_based = 2 * one_leg_h + (est_out[rt] + est_in[rt]) / 3600
    df.loc[rt, "block_h"] = np.minimum(span_based, distance_based)
    df.loc[rt, "cycles"] = 2
    df.loc[rt, "suspect"] = False
    df["roundtrip"] = rt

    # Olika fält men orimligt länge i luften = flera ben med osedda mellanlandningar
    # (uppmätt: GIG→NLU 198 h, YQM→CMH 35 h). Ersätt med distansmodellens blocktid;
    # timmar på osedda mellanben tappas hellre än att en "flygning" får dygn av blocktid.
    capped = ~rt & (df["airborne_h"] > 2 * df["model_airborne_h"] + 1.0)
    df.loc[capped, "block_h"] = df.loc[capped, "model_airborne_h"] + (est_out[capped] + est_in[capped]) / 3600
    df["capped"] = capped

    df["quality"] = np.select(
        [rt, capped, df["to_q"].eq("inferred"),
         df["off_q"].eq("obs") & df["on_q"].eq("obs"),
         df["to_q"].isin(["obs", "est"]) & df["ld_q"].isin(["obs", "est"])],
        ["roundtrip_inferred", "capped_model", "inferred", "full", "airborne_obs"], default="partial")

    off_utc = pd.to_datetime(df["off_block"], unit="s", utc=True)
    off_et = off_utc.dt.tz_convert(ET)
    df["off_block_utc"] = off_utc.dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df["on_block_utc"] = pd.to_datetime(df["on_block"], unit="s", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df["quarter"] = [quarter_label(t) for t in off_et]
    df["dep_local_hour_et"] = off_et.dt.hour
    info = {"airborne_a_min": a, "airborne_b_min_per_km": b, "taxi_out_global_min": g_out,
            "taxi_in_global_min": g_in, "taxi_out_airports": int(len(med_out)),
            "taxi_in_airports": int(len(med_in)), "turnaround_h": turnaround_h,
            "roundtrips": int(rt.sum())}
    return df, info


def _median_turnaround(df: pd.DataFrame, prm: Params) -> float:
    """Median gate-till-gate på utestation: nästa off-block − denna on-block för samma plan.

    Bara korta uppehåll (0,5–6 h) räknas — längre är dagsstopp på nav, inte vändningar."""
    d = df[df["to_q"].isin(["obs", "est"]) & df["ld_q"].isin(["obs", "est"])
           & (df["dep"] != df["arr"])].sort_values(["hex", "off_block"])
    nxt_off = d.groupby("hex")["off_block"].shift(-1)
    nxt_dep = d.groupby("hex")["dep"].shift(-1)
    gap_h = (nxt_off - d["on_block"]) / 3600
    ok = (nxt_dep == d["arr"]) & gap_h.between(0.5, 6.0)
    return float(gap_h[ok].median()) if ok.sum() >= 5 else prm.turnaround_default_h


# ---------- Bygg ----------

def _segment_hex(args) -> list[dict]:
    hexid, days = args
    pts = load_points(hexid, days)
    return segment(pts, default_lookup(), hexid)


def build(fleet: pd.DataFrame, days: list[date], workers: int = 6) -> tuple[pd.DataFrame, dict]:
    jobs = [(h, days) for h in fleet["hex"]]
    rows: list[dict] = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for hexid, res in zip(fleet["hex"], ex.map(_segment_hex, jobs)):
            LOG.info("%s: %d flygningar", hexid, len(res))
            rows.extend(res)
    raw = pd.DataFrame(rows)
    if raw.empty:
        return raw, {}
    df, info = fill(raw)
    df = df.merge(fleet[["hex", "registration", "type"]], on="hex", how="left")
    FLIGHTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(FLIGHTS_CSV, index=False)
    info["built_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return df, info


def _consecutive_runs(days: list[date]) -> list[list[date]]:
    runs: list[list[date]] = []
    for d in sorted(set(days)):
        if runs and (d - runs[-1][-1]).days == 1:
            runs[-1].append(d)
        else:
            runs.append([d])
    return runs


def build_sampled(fleet: pd.DataFrame, days: list[date], out_csv=None,
                  stitch_consecutive: bool = False) -> tuple[pd.DataFrame, dict]:
    """Stickprovsläge: varje (plan, dag) segmenteras SEPARAT och seriellt.

    Icke-angränsande dagar får aldrig sys ihop — annars blir "sista besöket dag D
    → första besöket dag D+7 på annan flygplats" en härledd flygning. Flygningar
    som korsar UTC-midnatt tappas i kanten; det sker lika mycket båda åren, så
    YoY på matchade veckodagar påverkas marginellt. Seriellt: inga
    processpooler (macOS spawn), ~40 plan × ~20 dagar går på några minuter."""
    lookup = default_lookup()
    rows: list[dict] = []
    # stitch_consecutive: sammanhängande dagar (hela veckor) segmenteras ihop så att
    # flygningar över UTC-midnatt inte tappas — används för nivåjämförelse mot rapporterat
    units = _consecutive_runs(days) if stitch_consecutive else [[d] for d in days]
    for unit in units:
        for hexid in fleet["hex"]:
            pts = load_points(hexid, unit)
            if len(pts):
                rows.extend(segment(pts, lookup, hexid))
    raw = pd.DataFrame(rows)
    if raw.empty:
        return raw, {}
    df, info = fill(raw)
    df = df.merge(fleet[["hex", "registration", "type"]], on="hex", how="left")
    df["off_date_utc"] = pd.to_datetime(df["off_block"], unit="s", utc=True).dt.date
    path = out_csv or (PROCESSED / "flights_sampled.csv.gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df, info


def read_flights_csv(path) -> pd.DataFrame:
    """Bara tom cell = saknat. pandas standard gör annars kontinentkoden "NA"
    (Nordamerika) och landskoden "NA" (Namibia) till NaN — uppmätt: CVG–MTY
    klassades då som charter i stället för ACMI."""
    return pd.read_csv(path, keep_default_na=False, na_values=[""])


def load() -> pd.DataFrame:
    return read_flights_csv(FLIGHTS_CSV)
