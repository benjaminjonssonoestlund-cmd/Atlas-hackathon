"""Cargojet (ICAO CJT) — live och historiska flygningar ur ADS-B.

HISTORIK: adsb.lol:s globe_history per plan och dag,
  https://adsb.lol/globe_history/Å/MM/DD/traces/<hex[-2:]>/trace_full_<hex>.json
samma väg och webbläsarheaders som frankea/adsbtrack använder. Uppmätt
2026-09-13: dagarna finns ~2 dygn efter; 404 betyder "planet flög inte / sågs
inte" för en publicerad dag, men "inte publicerad än" för de senaste dygnen —
därför cachas 404 som frånvaro först när dagen är gammal nog. adsb.fi och
airplanes.live gav 403/404 på samma väg och används inte. Dagsdumpar som
cjt_nowcast redan har sparat (samma readsb-format) läses direkt från disk.

LIVE: api.adsb.lol /v2/hex/<hela flottan> (fallback opendata.adsb.fi), och
spåret för de senaste timmarna ur adsb.lol /data/traces/…/trace_full_<hex>.json.
Samma live-spår spelas in var 15:e minut (raw/recent) så att historiken når
ända fram till nu, även de dygn globe_history inte har publicerat än.

Flygningar segmenteras ur spåren (markkontakt eller lucka) och får avgångs-
och ankomstflygplats från närmaste IATA-fält. Attribution: data © adsb.lol
och dess mottagarvärdar, ODbL 1.0.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
import urllib.error
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from ..cache import CACHE
from . import cjt_blockhours, cjt_tracker
from .base import DataSource

log = logging.getLogger("atlas.cargojet")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORE = DATA_DIR / "cargojet"
RECENT_DIR = STORE / "recent"
NOWCAST_TRACES = DATA_DIR.parent.parent / "cjt_nowcast" / "data" / "raw" / "adsb" / "traces"
NOWCAST_DAYS = NOWCAST_TRACES.parent / "days"     # kompletta dagsdumpar (cjt_nowcast extract)
INTERVALS_DIR = STORE / "intervals"                # luftburna intervall per spårfil

HISTORY_URL = "https://adsb.lol/globe_history/{d:%Y/%m/%d}/traces/{last2}/trace_full_{hex}.json"
LIVE_TRACE_URL = "https://adsb.lol/data/traces/{last2}/trace_full_{hex}.json"
LIVE_HOSTS = ("https://api.adsb.lol/v2/hex/", "https://opendata.adsb.fi/api/v2/hex/")

HISTORY_DAYS = int(os.environ.get("CARGOJET_HISTORY_DAYS", "30"))
PUBLISH_LAG_DAYS = 3        # 404 för yngre dagar kan betyda "ej publicerad än"
RETRY_RECENT_S = 3 * 3600
FETCH_PACE_S = 0.35
RECORD_EVERY_S = 15 * 60
RECENT_KEEP_S = 4 * 86400   # längre än publiceringsfördröjningen
GAP_PROBE = 10              # så många aktiva plan utan spår → dygnet saknas på adsb.lol

FT_TO_M = 0.3048
EARTH_KM = 6371.0
SPLIT_GAP_S = 45 * 60       # lucka som delar en flygning …
CRUISE_ALT_FT = 20000       # … om inte båda sidor är på marschhöjd
MIN_FLIGHT_S = 15 * 60
AIRPORT_MAX_KM = 40.0

# Samma headers som adsbtrack.fetcher._build_headers — utan dem svarar
# globe_history-värdarna 403.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")


def _headers(hexid: str) -> dict[str, str]:
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://adsb.lol/?icao={hexid}",
        "X-Requested-With": "XMLHttpRequest",
    }


# ---------- Referensdata ----------

def load_fleet() -> list[dict]:
    doc = json.loads((DATA_DIR / "cargojet_fleet.json").read_text(encoding="utf-8"))
    return doc["aircraft"]


class Airports:
    """Närmaste IATA-fält, vektoriserad haversine (~4 600 fält)."""

    def __init__(self, rows: list[list]):
        self.rows = rows
        self._lat = np.radians([r[3] for r in rows])
        self._lon = np.radians([r[4] for r in rows])

    @classmethod
    def load(cls) -> "Airports":
        doc = json.loads((DATA_DIR / "airports_min.json").read_text(encoding="utf-8"))
        return cls(doc["airports"])

    def nearest(self, lat: float, lon: float, max_km: float = AIRPORT_MAX_KM) -> dict | None:
        la, lo = np.radians(lat), np.radians(lon)
        a = (np.sin((self._lat - la) / 2) ** 2
             + np.cos(la) * np.cos(self._lat) * np.sin((self._lon - lo) / 2) ** 2)
        km = 2 * EARTH_KM * np.arcsin(np.sqrt(a))
        i = int(np.argmin(km))
        if km[i] > max_km:
            return None
        iata, icao, name, alat, alon = self.rows[i]
        return {"iata": iata, "icao": icao, "name": name, "lat": alat, "lon": alon}


# ---------- Segmentering (ren funktion, testbar) ----------

def _points(doc: dict) -> list[tuple]:
    """readsb trace_full → [(t, lat, lon, alt_ft|None, on_ground, gs, track, callsign)]."""
    ts = float(doc.get("timestamp") or 0)
    out = []
    callsign = ""
    for p in doc.get("trace") or []:
        if len(p) < 6 or p[1] is None or p[2] is None:
            continue
        detail = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
        if detail and (detail.get("flight") or "").strip():
            callsign = detail["flight"].strip()
        alt = p[3]
        ground = alt == "ground"
        out.append((ts + float(p[0]), float(p[1]), float(p[2]),
                    None if ground or alt is None else float(alt), ground,
                    p[4], p[5], callsign))
    return out


def _km(lat1, lon1, lat2, lon2) -> float:
    la1, lo1, la2, lo2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((la2 - la1) / 2) ** 2 + np.cos(la1) * np.cos(la2) * np.sin((lo2 - lo1) / 2) ** 2
    return float(2 * EARTH_KM * np.arcsin(np.sqrt(a)))


def _split_runs(points: list[tuple]) -> list[list[tuple]]:
    """Luftburna följder: bryts av markkontakt, eller av en lucka > 45 min utom
    täckningshål på marschhöjd med rimlig implicerad fart."""
    points = sorted(points, key=lambda p: p[0])
    runs: list[list[tuple]] = []
    cur: list[tuple] = []
    for p in points:
        if p[4]:                                   # markpunkt avslutar
            if cur:
                runs.append(cur)
                cur = []
            continue
        if cur:
            prev = cur[-1]
            gap = p[0] - prev[0]
            if gap > SPLIT_GAP_S:
                high = (prev[3] or 0) >= CRUISE_ALT_FT and (p[3] or 0) >= CRUISE_ALT_FT
                kn = _km(prev[1], prev[2], p[1], p[2]) / 1.852 / (gap / 3600)
                if not (high and 200 <= kn <= 650):
                    runs.append(cur)
                    cur = []
        cur.append(p)
    if cur:
        runs.append(cur)
    return runs


def airborne_intervals(points: list[tuple]) -> list[list]:
    """[t0, t1, max_alt_ft] per luftburen följd — kompakt underlag för flygtimmar."""
    return [[int(r[0][0]), int(r[-1][0]), int(max((p[3] or 0) for p in r))]
            for r in _split_runs(points) if len(r) >= 2]


def _utc_day(ts: float | None) -> date | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).date()


def segment(points: list[tuple], airports: Airports | None = None,
            max_path_points: int = 240) -> list[dict]:
    """Dela ett sammanhängande spår i flygningar.

    En flygning är en följd luftburna punkter. Den bryts av markkontakt, eller
    av en lucka > 45 min — utom när båda sidor ligger på marschhöjd och den
    implicerade farten är rimlig (täckningshål över hav/glesbygd)."""
    out = []
    for f in _split_runs(points):
        t0, t1 = f[0][0], f[-1][0]
        max_alt = max((p[3] or 0) for p in f)
        if t1 - t0 < MIN_FLIGHT_S or len(f) < 10 or max_alt < 5000:
            continue
        calls = Counter(p[7] for p in f if p[7])
        cs = calls.most_common(1)[0][0] if calls else ""
        # Avgång/ankomst bara om spåret börjar/slutar lågt — annars är det
        # ett täckningshål och fältet är okänt.
        dep = airports.nearest(f[0][1], f[0][2]) if airports and (f[0][3] or 0) < 12000 else None
        arr = airports.nearest(f[-1][1], f[-1][2]) if airports and (f[-1][3] or 0) < 12000 else None
        stride = max(1, -(-len(f) // max_path_points))   # uppåtavrundat → ≤ max punkter
        sampled = f[::stride]
        if sampled[-1] is not f[-1]:
            sampled.append(f[-1])
        out.append({
            "callsign": cs,
            "t0": int(t0), "t1": int(t1),
            "dur_min": round((t1 - t0) / 60),
            "max_alt_ft": int(max_alt),
            "dep": dep, "arr": arr,
            "path": [[round(p[2], 4), round(p[1], 4),
                      int(round((p[3] or 0) * FT_TO_M, -1)), int(p[0]),
                      None if p[6] is None else round(float(p[6]))]
                     for p in sampled],
        })
    return out


def merge_recent(existing: list[list], doc: dict, keep_after: float) -> list[list]:
    """Slå ihop ett live-spår (relativa tider) med inspelade punkter (absoluta tider).

    Dedupe på tid; punkter äldre än `keep_after` rensas."""
    ts = float(doc.get("timestamp") or 0)
    by_t = {round(float(p[0]), 1): p for p in existing if p[0] >= keep_after}
    for p in doc.get("trace") or []:
        q = [ts + float(p[0]), *p[1:]]
        if q[0] >= keep_after:
            by_t[round(q[0], 1)] = q
    return [by_t[k] for k in sorted(by_t)]


# ---------- Källan ----------

class CargojetSource(DataSource):
    name = "cargojet"
    description = "Cargojet (CJT) — live + historik ur adsb.lol (ODbL 1.0)"

    def __init__(self) -> None:
        self.fleet = load_fleet()
        self.by_hex = {a["hex"]: a for a in self.fleet}
        self._airports: Airports | None = None
        self._lock = threading.Lock()
        self._flight_cache: dict[tuple, tuple[tuple, list[dict]]] = {}
        self._iv_mem: dict[Path, tuple[float, list]] = {}
        self._thread: threading.Thread | None = None
        self.block_ledger = cjt_blockhours.Ledger()
        self.status = {"running": False, "pass_started": None, "fetched": 0,
                       "with_data": 0, "errors": 0, "blocked_until": None,
                       "current_day": None, "day_index": None, "plan_days": None,
                       "unavailable_days": 0}

    @property
    def airports(self) -> Airports:
        if self._airports is None:
            self._airports = Airports.load()
        return self._airports

    def check(self) -> bool:
        return bool(self.fleet)

    # ----- disklager -----

    @staticmethod
    def _day_dir(d: date) -> Path:
        return STORE / "traces" / d.isoformat()

    def _index(self, d: date) -> dict:
        path = self._day_dir(d) / "_index.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def _write_index(self, d: date, idx: dict) -> None:
        path = self._day_dir(d) / "_index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(idx))

    def _trace_files(self, hexid: str, since: float | None = None,
                     until: float | None = None) -> list[Path]:
        """Spårfiler för ett plan, valfritt begränsade till ett tidsfönster (±1 UTC-dygn)."""
        lo = _utc_day(since) - timedelta(days=1) if since is not None else None
        hi = _utc_day(until) + timedelta(days=1) if until is not None else None

        def inside(d: date) -> bool:
            return (lo is None or d >= lo) and (hi is None or d <= hi)

        files = [f for f in (STORE / "traces").glob(f"*/{hexid}.json.gz")
                 if inside(date.fromisoformat(f.parent.name))]
        files += [f for f in NOWCAST_TRACES.glob(f"*/*/*/{hexid}.json.gz")
                  if inside(date(int(f.parts[-4]), int(f.parts[-3]), int(f.parts[-2])))]
        recent = RECENT_DIR / f"{hexid}.json.gz"
        if recent.exists() and (until is None or until >= time.time() - RECENT_KEEP_S):
            files.append(recent)
        return sorted(files)

    def _needs_fetch(self, d: date, hexid: str, idx: dict, today: date) -> bool:
        if (NOWCAST_TRACES / f"{d:%Y/%m/%d}" / f"{hexid}.json.gz").exists():
            return False
        entry = idx.get(hexid)
        if entry is None:
            return True
        if entry["status"] == 200:
            return False
        if entry["status"] == 404 and (today - d).days >= PUBLISH_LAG_DAYS:
            return False
        return time.time() - entry["at"] > RETRY_RECENT_S

    # ----- historisk hämtning -----

    def fetch_day(self, d: date, hexid: str) -> int:
        url = HISTORY_URL.format(d=d, last2=hexid[-2:], hex=hexid)
        try:
            raw = self.http_get(url, headers=_headers(hexid))
            status = 200
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = None
        if raw:
            doc = json.loads(raw)
            if doc.get("trace"):
                path = self._day_dir(d) / f"{hexid}.json.gz"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(gzip.compress(json.dumps(doc, separators=(",", ":")).encode()))
            else:
                status = 404
        if status in (200, 404):
            idx = self._index(d)
            idx[hexid] = {"status": status, "at": time.time()}
            self._write_index(d, idx)
        return status

    def backfill_plan(self, now: datetime | None = None) -> list[date]:
        """UTC-dygn att hämta: senaste HISTORY_DAYS, sedan innevarande kvartal hittills
        (blocktimmarna). Fjolårens jämförelse tas ur MD&A, inte ADS-B."""
        now_et = (now or datetime.now(timezone.utc)).astimezone(cjt_tracker.ET)
        today = now_et.astimezone(timezone.utc).date()
        days = [today - timedelta(days=b) for b in range(1, HISTORY_DAYS + 1)]
        year, q = cjt_tracker.quarter_of(now_et.date())
        start = cjt_tracker.quarter_start(year, q).date() - timedelta(days=1)   # UTC-dygnet före bär 00–04 ET
        d = today - timedelta(days=1)
        while d >= start:
            days.append(d)
            d -= timedelta(days=1)
        return list(dict.fromkeys(days))

    def backfill_once(self) -> None:
        today = datetime.now(timezone.utc).date()
        plan = self.backfill_plan()
        # Mest aktiva planen först: då avslöjas ett dygn som saknas på servern snabbt.
        activity = Counter(f.name.split(".")[0] for f in (STORE / "traces").glob("*/*.json.gz"))
        fleet = sorted(self.fleet, key=lambda a: -activity[a["hex"]])
        st = self.status
        st.update(pass_started=time.time(), fetched=0, with_data=0, errors=0,
                  plan_days=len(plan), unavailable_days=0)
        for i, d in enumerate(plan):
            st["current_day"], st["day_index"] = d.isoformat(), i
            if (NOWCAST_DAYS / f"{d:%Y%m%d}.json").exists():
                continue                         # hel dagsdump redan extraherad av cjt_nowcast
            idx = self._index(d)
            if idx.get("_unavailable"):
                st["unavailable_days"] += 1
                continue
            has_data = any(isinstance(v, dict) and v.get("status") == 200
                           for k, v in idx.items() if not k.startswith("_"))
            misses = 0
            for a in fleet:
                if a.get("active_to") and a["active_to"] < d.isoformat():
                    continue
                if not self._needs_fetch(d, a["hex"], idx, today):
                    continue
                try:
                    code = self.fetch_day(d, a["hex"])
                except Exception as exc:  # noqa: BLE001 — nätfel: nästa varv
                    st["errors"] += 1
                    log.debug("cargojet %s %s: %s", d, a["hex"], exc)
                    code = None
                st["fetched"] += 1
                if code == 200:
                    st["with_data"] += 1
                    has_data = True
                elif code == 404:
                    misses += 1
                elif code in (403, 429):
                    # Artig brytare: värden strypte oss — vänta i stället för att hamra.
                    st["blocked_until"] = time.time() + 600
                    log.warning("cargojet: adsb.lol svarade %s, pausar 10 min", code)
                    time.sleep(600)
                    st["blocked_until"] = None
                time.sleep(FETCH_PACE_S)
                if not has_data and misses >= GAP_PROBE and (today - d).days >= PUBLISH_LAG_DAYS:
                    # Serverlucka (t.ex. jul–aug 2026): inte ens de mest aktiva planen finns.
                    idx = self._index(d)
                    idx["_unavailable"] = {"at": time.time(), "probed": misses}
                    self._write_index(d, idx)
                    st["unavailable_days"] += 1
                    break
        st["current_day"], st["day_index"] = None, None
        log.info("cargojet-historik: %d anrop, %d dagar med spår, %d fel",
                 st["fetched"], st["with_data"], st["errors"])

    # ----- inspelning av live-spår (når fram till nu) -----

    def record_recent_once(self) -> int:
        RECENT_DIR.mkdir(parents=True, exist_ok=True)
        keep_after = time.time() - RECENT_KEEP_S
        n = 0
        for a in self.fleet:
            hexid = a["hex"]
            try:
                doc = self.http_get_json(LIVE_TRACE_URL.format(last2=hexid[-2:], hex=hexid),
                                         headers=_headers(hexid))
            except urllib.error.HTTPError:
                doc = None                          # 404 = inte sett de senaste timmarna
            except Exception as exc:  # noqa: BLE001 — nätfel: nästa varv
                log.debug("cargojet recent %s: %s", hexid, exc)
                doc = None
            path = RECENT_DIR / f"{hexid}.json.gz"
            try:
                old = json.loads(gzip.decompress(path.read_bytes()))["trace"] if path.exists() else []
            except (OSError, ValueError, KeyError):
                old = []
            merged = merge_recent(old, doc or {}, keep_after)
            if merged != old:
                path.write_bytes(gzip.compress(json.dumps(
                    {"icao": hexid, "timestamp": 0, "trace": merged},
                    separators=(",", ":")).encode()))
                n += 1
            time.sleep(FETCH_PACE_S)
        return n

    def ensure_backfill(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return

            def _record_loop():
                while True:
                    try:
                        n = self.record_recent_once()
                        log.info("cargojet: live-spår inspelade för %d plan", n)
                    except Exception as exc:  # noqa: BLE001 — tråden får aldrig dö
                        log.warning("cargojet-inspelning: %s", exc)
                    try:
                        self.update_block_hours()      # sparar även pågående dygns timmar
                    except Exception as exc:  # noqa: BLE001 — tråden får aldrig dö
                        log.warning("cargojet-blocktimmar: %s", exc)
                    time.sleep(RECORD_EVERY_S)

            threading.Thread(target=_record_loop, daemon=True, name="cargojet-recent").start()

            def _loop():
                self.status["running"] = True
                while True:
                    try:
                        n = self.index_intervals_once()
                        log.info("cargojet: flygtidsindex klart för %d spårfiler", n)
                        self.backfill_once()
                    except Exception as exc:  # noqa: BLE001 — tråden får aldrig dö
                        log.warning("cargojet-backfill: %s", exc)
                    time.sleep(3600)

            self._thread = threading.Thread(target=_loop, daemon=True, name="cargojet-backfill")
            self._thread.start()

    # ----- flygningar -----

    def flights_for_hex(self, hexid: str, since: float | None = None,
                        until: float | None = None) -> list[dict]:
        files = self._trace_files(hexid, since, until)
        sig = tuple((str(f), f.stat().st_mtime) for f in files)
        key = (hexid, _utc_day(since), _utc_day(until))
        cached = self._flight_cache.get(key)
        if cached and cached[0] == sig:
            return cached[1]
        pts: dict[float, tuple] = {}
        for f in files:
            try:
                doc = json.loads(gzip.decompress(f.read_bytes()))
            except (OSError, ValueError):
                continue
            for p in _points(doc):
                pts[p[0]] = p                      # dagsgränser överlappar → dedupe på tid
        flights = segment(list(pts.values()), self.airports)
        a = self.by_hex.get(hexid, {})
        for fl in flights:
            fl.update(hex=hexid, reg=a.get("reg", ""), type=a.get("type", ""),
                      id=f"{hexid}-{fl['t0']}")
        self._flight_cache[key] = (sig, flights)
        return flights

    def history(self, since: float, until: float | None = None,
                max_path_points: int = 240) -> dict:
        until = until or time.time()
        flights = []
        for a in self.fleet:
            for fl in self.flights_for_hex(a["hex"], since, until):
                if fl["t1"] >= since and fl["t0"] <= until:
                    flights.append(fl)
        flights.sort(key=lambda f: f["t0"], reverse=True)
        stride_out = []
        for fl in flights:
            path = fl["path"]
            if len(path) > max_path_points:
                step = len(path) / max_path_points
                path = [path[int(i * step)] for i in range(max_path_points)] + [path[-1]]
            stride_out.append({**fl, "path": path})
        return {"flights": stride_out, "since": int(since), "until": int(until)}

    def archive_days(self) -> list[dict]:
        """Dagar med sparade spår (egen hämtning + cjt_nowcast-dumpar)."""
        counts: Counter = Counter()
        for f in (STORE / "traces").glob("*/*.json.gz"):
            counts[f.parent.name] += 1
        for f in NOWCAST_TRACES.glob("*/*/*/*.json.gz"):
            counts["-".join(f.parts[-4:-1])] += 1
        return [{"day": d, "aircraft": n} for d, n in sorted(counts.items(), reverse=True)]

    # ----- flygtimmar (kompakta luftburna intervall per spårfil) -----

    @staticmethod
    def _sidecar(path: Path) -> Path:
        rel = path.relative_to(STORE) if STORE in path.parents else \
            Path("nowcast") / path.relative_to(NOWCAST_TRACES)
        return INTERVALS_DIR / rel.parent / (path.name.split(".")[0] + ".json")

    def _file_intervals(self, path: Path) -> list[list]:
        """Luftburna intervall för en spårfil — cachade i minnet och som sidofil på disk."""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        hit = self._iv_mem.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        side = self._sidecar(path)
        try:
            doc = json.loads(side.read_text())
            if doc.get("mtime") == mtime:
                self._iv_mem[path] = (mtime, doc["iv"])
                return doc["iv"]
        except (OSError, ValueError, KeyError):
            pass
        try:
            iv = airborne_intervals(_points(json.loads(gzip.decompress(path.read_bytes()))))
        except (OSError, ValueError):
            return []
        side.parent.mkdir(parents=True, exist_ok=True)
        tmp = side.with_name(f"{side.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"mtime": mtime, "iv": iv}))
        tmp.replace(side)
        self._iv_mem[path] = (mtime, iv)
        return iv

    def index_intervals_once(self) -> int:
        files = list((STORE / "traces").glob("*/*.json.gz")) + list(NOWCAST_TRACES.glob("*/*/*/*.json.gz"))
        for f in files:
            self._file_intervals(f)
        return len(files)

    def intervals_for_window(self, start: datetime, end: datetime) -> dict[str, list[list]]:
        s, e = start.timestamp(), end.timestamp()
        return {a["hex"]: [iv for f in self._trace_files(a["hex"], s, e) for iv in self._file_intervals(f)]
                for a in self.fleet}

    # ----- blocktimmar (ledger på disk, se cjt_blockhours) -----

    @staticmethod
    def _recent_since() -> float | None:
        """Äldsta punkt i live-inspelningen — från och med den är varje dygn täckt."""
        firsts = []
        for f in RECENT_DIR.glob("*.json.gz"):
            try:
                trace = json.loads(gzip.decompress(f.read_bytes()))["trace"]
            except (OSError, ValueError, KeyError):
                continue
            if trace:
                firsts.append(float(trace[0][0]))
        return min(firsts) if firsts else None

    def _utc_day_available(self, d: date, recent_since: float | None) -> bool:
        if (NOWCAST_DAYS / f"{d:%Y%m%d}.json").exists():
            return True
        if any(self._day_dir(d).glob("*.json.gz")):
            return True
        day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
        return recent_since is not None and recent_since <= day_start

    def _complete_day_fn(self):
        """ET-dygn d är komplett om UTC-dygnen d och d+1 har data (ET = UTC−4/−5)."""
        recent_since = self._recent_since()
        return lambda d: (self._utc_day_available(d, recent_since)
                          and self._utc_day_available(d + timedelta(days=1), recent_since))

    def update_block_hours(self) -> None:
        """Mät innevarande kvartal hittills in i ledgern; kalibrera om var 6:e timme."""
        now = datetime.now(timezone.utc)
        complete_day = self._complete_day_fn()
        cjt_blockhours.update_ledger(self.block_ledger, now, self.intervals_for_window, complete_day)
        cal = self.block_ledger.doc.get("calibration") or {}
        year, q = cjt_tracker.quarter_of(now.astimezone(cjt_tracker.ET).date())
        if cal.get("for_quarter") != f"{year}Q{q}" or time.time() - cal.get("computed_at", 0) > 6 * 3600:
            cal = cjt_blockhours.compute_calibration(now, self.intervals_for_window, complete_day,
                                                     cjt_blockhours.load_reported())
            with self.block_ledger.lock:
                self.block_ledger.doc["calibration"] = cal
                self.block_ledger.save()
            log.info("cargojet: blocktimskalibrering %s (%s)", cal.get("factor"), cal.get("ratios"))

    def quarter_to_date(self) -> dict:
        """Blocktimmar innevarande kvartal hittills mot rapporterat samma tidpunkt 1–2 år bakåt."""
        def _compute():
            if time.time() - self.block_ledger.doc.get("updated_at", 0) > 2 * RECORD_EVERY_S:
                self.update_block_hours()              # kallstart eller stoppad inspelningstråd
            with self.block_ledger.lock:
                doc = json.loads(json.dumps(self.block_ledger.doc))
            return cjt_blockhours.quarter_to_date(datetime.now(timezone.utc), doc,
                                                  cjt_blockhours.load_reported(), doc.get("calibration"))
        return CACHE.get_or_fetch("cargojet:qtd:v2", 60, _compute) or \
            {"error": "blocktimmarna kunde inte beräknas än"}

    # ----- live -----

    def fetch_live(self) -> list[dict]:
        hexes = ",".join(a["hex"] for a in self.fleet)
        data = None
        last_exc: Exception | None = None
        for host in LIVE_HOSTS:
            try:
                data = self.http_get_json(host + hexes)
                break
            except Exception as exc:  # noqa: BLE001 — prova nästa värd
                last_exc = exc
        if data is None:
            raise last_exc or RuntimeError("ingen live-värd svarade")
        items = []
        for ac in data.get("ac") or []:
            hexid = (ac.get("hex") or "").lower()
            if ac.get("lat") is None or ac.get("lon") is None:
                continue
            a = self.by_hex.get(hexid, {})
            alt = ac.get("alt_baro")
            items.append({
                "hex": hexid,
                "callsign": (ac.get("flight") or "").strip() or a.get("reg", hexid),
                "reg": ac.get("r") or a.get("reg", ""),
                "type": a.get("type") or ac.get("t") or "",
                "lat": ac["lat"], "lon": ac["lon"],
                "on_ground": alt == "ground",
                "alt_ft": None if alt in (None, "ground") else int(alt),
                "gs_kn": ac.get("gs"), "track": ac.get("track"),
                "seen_s": ac.get("seen_pos", ac.get("seen")),
            })
        return items

    def live_trail(self, hexid: str) -> dict | None:
        """Dagens spår för ett plan i luften → avgångsfält + svans."""
        def _fetch():
            doc = self.http_get_json(LIVE_TRACE_URL.format(last2=hexid[-2:], hex=hexid),
                                     headers=_headers(hexid))
            flights = segment(_points(doc), self.airports, max_path_points=300)
            return flights[-1] if flights else None
        return CACHE.get_or_fetch(f"cargojet:trail:{hexid}", 60, _fetch)

    def live(self) -> dict:
        def _compute():
            items = self.fetch_live()
            for it in items:
                if it["on_ground"]:
                    continue
                try:
                    trail = self.live_trail(it["hex"])
                except Exception:  # noqa: BLE001 — svansen är bonus
                    trail = None
                if trail and time.time() - trail["t1"] < 1800:
                    it["dep"] = trail["dep"]
                    it["t0"] = trail["t0"]
                    it["path"] = trail["path"]
            return {"items": items, "fleet_size": len(self.fleet), "ts": time.time(),
                    "attribution": "ADS-B: adsb.lol / adsb.fi (ODbL 1.0)"}
        return CACHE.get_or_fetch("cargojet:live", 30, _compute) or \
            {"items": [], "fleet_size": len(self.fleet), "ts": time.time()}
