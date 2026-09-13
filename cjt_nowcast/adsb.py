"""Historiska ADS-B-spår för Cargojet — adsb.lol globe_history-dumpar (ODbL 1.0).

KÄLLVÄG: dagsdumparna på GitHub (adsblol/globe_history_ÅÅÅÅ, en release per
dag, split-tar på 2–3,7 GB). Varje dag strömmas utan att sparas och bara
Cargojet-relevanta spår skrivs till disk:
  - hex i den kända flottan (TC-registret), eller
  - callsign som börjar på CJT (Cargojets ICAO-kod), eller
  - ownOp innehåller "Cargojet".
Det gör flottupptäckten komplett PER DAG: plan som lämnat registret och
inhyrda plan med utländsk hex kommer med automatiskt.

VARFÖR INTE adsb.lol:s HTTP-sökväg per plan (/globe_history/Å/M/D/traces/…):
uppmätt 2026-09-13 finns den bara för ~jan 2024–sep 2025. Från ~dec 2025 ger
hela dagkatalogen 404, och efter några hundra anrop kom 403. En 404 där
betyder alltså "dagen saknas på servern", inte "planet flög inte" — att
cacha det som frånvaro skulle tyst radera blocktimmar. Dumparna är samma
dataset och täcker hela perioden.

Dag-manifestet raw/adsb/days/<ÅÅÅÅMMDD>.json skrivs SIST och är markören för
att dagen är komplett. Ett hex som saknas i ett komplett manifest sågs inte
den dagen; en dag utan manifest är ohämtad.

Spårformat (readsb trace_full): {"icao", "r", "t", "ownOp", "timestamp",
"trace": [[sek_efter_timestamp, lat, lon, alt_baro|"ground"|null, gs, track,
flags, baro_rate, detaljer|null, ...], ...]}.

Attribution: data © adsb.lol och dess mottagarvärdar, ODbL 1.0.
"""

from __future__ import annotations

import bisect
import gzip
import io
import json
import logging
import re
import tarfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .common import RAW, UA, NotFound, fetch_cached, http_get

LOG = logging.getLogger("cjt.adsb")

RELEASES_MD = "https://raw.githubusercontent.com/adsblol/globe_history_{y}/main/RELEASES.md"
TAG = "v{d:%Y.%m.%d}-planes-readsb-{variant}"
ASSET_URL = "https://github.com/adsblol/globe_history_{y}/releases/download/{tag}/{tag}.tar{part}"
# README:n: använd prod, om inte staging är märkbart större (prod låg nere den dagen)
STAGING_PREFERRED_RATIO = 1.2

ADSB_DIR = RAW / "adsb"
TRACE_DIR = ADSB_DIR / "traces"
DAY_DIR = ADSB_DIR / "days"
RELEASE_DIR = ADSB_DIR / "releases"

_TRACE_NAME = re.compile(r"traces/[0-9a-f~]{2}/trace_full_(~?[0-9a-f]{6})\.json$")


# ---------- Filer ----------

def trace_file(hexid: str, day: date) -> Path:
    return TRACE_DIR / f"{day:%Y/%m/%d}" / f"{hexid.lower()}.json.gz"


def day_manifest_file(day: date) -> Path:
    return DAY_DIR / f"{day:%Y%m%d}.json"


def day_manifest(day: date) -> dict | None:
    p = day_manifest_file(day)
    return json.loads(p.read_text()) if p.exists() else None


def is_day_done(day: date) -> bool:
    return day_manifest_file(day).exists()


def load_trace(hexid: str, day: date) -> dict | None:
    p = trace_file(hexid, day)
    if not p.exists() or not is_day_done(day):
        return None
    return json.loads(gzip.decompress(p.read_bytes()))


# ---------- Releaseindex ----------

def release_index(year: int, refresh: bool = False) -> dict[date, dict[str, tuple[int, str]]]:
    """{dag: {"prod": (MiB, taggsuffix), "staging": ...}} ur repots RELEASES.md.

    Taggsuffixet är normalt "0" men "0tmp" för 2025-05-28…06-10. Dagar med
    bara "mlatonly" (2025-12-14…17, 2026-05-06) saknar ADS-B och får inget
    index — de är riktiga hål som valideringen justerar för.

    Innevarande års index växer dagligen — hämtas om när en efterfrågad dag
    saknas (se `choose_release`)."""
    cached = sorted(RELEASE_DIR.glob(f"RELEASES_{year}_*.md"))
    if cached and not refresh:
        path = cached[-1]
    else:
        path = fetch_cached(RELEASES_MD.format(y=year),
                            RELEASE_DIR / f"RELEASES_{year}_{date.today():%Y%m%d}.md", refresh=True)
    out: dict[date, dict[str, tuple[int, str]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"- (\d{4}-\d{2}-\d{2}) (.*)", line)
        if m:
            found = re.findall(r"planes-readsb-(prod|staging)-(0(?:tmp)?) \((\d+) MiB\)", m.group(2))
            if found:
                out[date.fromisoformat(m.group(1))] = {k: (int(mib), sfx) for k, sfx, mib in found}
    return out


def choose_release(day: date) -> tuple[str, int] | None:
    """(variant, MiB), där variant är taggdelen efter "planes-readsb-", t.ex. "prod-0"."""
    idx = release_index(day.year)
    if day not in idx and day >= date.today().replace(month=1, day=1):
        idx = release_index(day.year, refresh=True)
    sizes = idx.get(day)
    if not sizes:
        return None
    prod, staging = sizes.get("prod", (0, "0")), sizes.get("staging", (0, "0"))
    if prod[0] and staging[0] <= prod[0] * STAGING_PREFERRED_RATIO:
        return f"prod-{prod[1]}", prod[0]
    return (f"staging-{staging[1]}", staging[0]) if staging[0] else (f"prod-{prod[1]}", prod[0])


def _exists(url: str) -> bool:
    # Range-GET i stället för HEAD: urllib gör om HEAD till GET vid redirect
    # (GitHub → objektlagring) och skulle då ladda ner hela delen.
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=60):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def asset_urls(day: date, variant: str) -> list[str]:
    cache = RELEASE_DIR / f"assets_{day:%Y%m%d}_{variant}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    tag = TAG.format(d=day, variant=variant)
    urls = []
    for a in "abcdefghij":
        url = ASSET_URL.format(y=day.year, tag=tag, part=f".a{a}")
        if not _exists(url):
            break
        urls.append(url)
    if not urls:
        single = ASSET_URL.format(y=day.year, tag=tag, part="")
        urls = [single] if _exists(single) else []
    if urls:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(urls))
    return urls


# ---------- Strömning ----------

PROBE_WINDOW = 1 << 18
TRACE_MARKER = "/traces/"


def part_sizes(urls: list[str]) -> list[int]:
    sizes = []
    for u in urls:
        req = urllib.request.Request(u, headers={"User-Agent": UA, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            sizes.append(int(r.headers["Content-Range"].rsplit("/", 1)[1]))
    return sizes


def ranged_reader(urls: list[str], sizes: list[int]):
    """read(offset, n) över de konkatenerade delarna, via HTTP Range."""
    bounds = [0]
    for s in sizes:
        bounds.append(bounds[-1] + s)

    def read(off: int, n: int) -> bytes:
        out = b""
        while n > 0 and off < bounds[-1]:
            i = bisect.bisect_right(bounds, off) - 1
            local = off - bounds[i]
            take = min(n, sizes[i] - local)
            req = urllib.request.Request(urls[i], headers={
                "User-Agent": UA, "Range": f"bytes={local}-{local + take - 1}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                chunk = r.read()
            if not chunk:
                break
            out += chunk
            off += len(chunk)
            n -= len(chunk)
        return out
    return read


def _header_in(buf: bytes, base: int) -> tuple[int, tarfile.TarInfo] | None:
    """Första giltiga tar-huvud i buf (bara på 512-gränser räknat från dumpens start)."""
    for i in range((-base) % 512, len(buf) - 511, 512):
        blk = buf[i:i + 512]
        if blk[257:262] != b"ustar":
            continue
        try:
            return base + i, tarfile.TarInfo.frombuf(blk, "utf-8", "surrogateescape")
        except tarfile.HeaderError:
            continue
    return None


def traces_start(read, total: int) -> int:
    """Byteoffset till första spårfilen i dumpen; 0 (= strömma allt) om layouten är okänd.

    Uppmätt 2026-06-17: de första ~30 % (~1 GiB) av dumpen är annat än
    traces/ (heatmaps m.m.), och traces/ löper sedan till slutet. Att hoppa
    över förspelet sparar ~30 % av ~2,5 TB. Binärsökning på fönster om 256 KB
    (ett fönster inne i traces/ innehåller alltid ett spårhuvud, spårfiler är
    5–50 KB), sedan exakt stegning huvud för huvud från sista icke-spårhuvudet.
    """
    def probe(off: int):
        off -= off % 512
        return _header_in(read(off, PROBE_WINDOW), off)

    first, tail = probe(0), probe(int(total * 0.6))
    if not first or not tail or TRACE_MARKER not in tail[1].name or TRACE_MARKER in first[1].name:
        return 0
    lo, hi, best = 0, int(total * 0.6), first
    while hi - lo > PROBE_WINDOW:
        mid = (lo + hi) // 2
        h = probe(mid)
        if h and TRACE_MARKER in h[1].name:
            hi = mid
        else:
            lo = mid
            if h and h[0] > best[0]:
                best = h
    off, ti = best
    for _ in range(5000):
        if TRACE_MARKER in ti.name:
            return off
        off += 512 + (ti.size + 511) // 512 * 512
        h = _header_in(read(off, 512), off)
        if h is None or h[0] != off:
            return 0
        ti = h[1]
    return 0


class _ConcatHTTP(io.RawIOBase):
    """Läs flera HTTP-svar i följd som en ström (dumpen är split-tar).

    parts: [(url, startbyte)] — startbyte > 0 läses med Range."""

    def __init__(self, parts: list[tuple[str, int]]):
        self._parts = list(parts)
        self._resp = None
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:
        while True:
            if self._resp is None:
                if not self._parts:
                    return 0
                url, start = self._parts.pop(0)
                headers = {"User-Agent": UA}
                if start:
                    headers["Range"] = f"bytes={start}-"
                self._resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300)
            n = self._resp.readinto(buf)
            if n:
                self.bytes_read += n
                return n
            self._resp.close()
            self._resp = None


def _cargojet_match(raw: bytes, known: bool) -> dict | None:
    """Metadata om spåret är Cargojet-relevant. `known` = hex i flottlistan."""
    if not known and b"argojet" not in raw and b'"CJT' not in raw:
        return None
    j = json.loads(raw)
    callsigns = sorted({(p[8].get("flight") or "").strip() for p in j.get("trace", [])
                        if len(p) > 8 and isinstance(p[8], dict)} - {""})
    cjt = [c for c in callsigns if c.startswith("CJT")]
    own = j.get("ownOp") or ""
    by = [k for k, hit in (("fleet", known), ("callsign", bool(cjt)),
                          ("ownOp", "cargojet" in own.lower())) if hit]
    if not by:
        return None
    return {"hex": (j.get("icao") or "").lower(), "registration": j.get("r", ""),
            "icao_type": j.get("t", ""), "ownOp": own, "callsigns": callsigns[:20],
            "cjt_callsigns": cjt, "points": len(j.get("trace", [])), "matched_by": by}


def extract_day(day: date, fleet_hexes: set[str], retries: int = 3) -> dict:
    """Strömma en dagsdump och spara alla Cargojet-relevanta spår. Idempotent."""
    done = day_manifest(day)
    if done:
        return done
    rel = choose_release(day)
    if rel is None:
        return {"day": day.isoformat(), "status": "no_release"}
    variant, mib = rel
    urls = asset_urls(day, variant)
    if not urls:
        return {"day": day.isoformat(), "status": "no_assets", "variant": variant}
    sizes = part_sizes(urls)
    skip = traces_start(ranged_reader(urls, sizes), sum(sizes))
    parts, acc = [], 0
    for u, s in zip(urls, sizes):
        if skip < acc + s:
            parts.append((u, max(0, skip - acc)))
        acc += s

    for attempt in range(retries + 1):
        t0 = time.monotonic()
        found: dict[str, dict] = {}
        scanned = 0
        stream = _ConcatHTTP(parts)
        try:
            with tarfile.open(fileobj=io.BufferedReader(stream, buffer_size=1 << 20), mode="r|") as tar:
                for member in tar:
                    m = _TRACE_NAME.search(member.name)
                    if not member.isfile() or not m:
                        continue
                    scanned += 1
                    hexid = m.group(1).lower()
                    data = tar.extractfile(member).read()
                    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
                    hit = _cargojet_match(raw, hexid in fleet_hexes)
                    if not hit:
                        continue
                    hit["hex"] = hit["hex"] or hexid
                    dest = trace_file(hit["hex"], day)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data if data[:2] == b"\x1f\x8b" else gzip.compress(raw))
                    found[hit["hex"]] = hit
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError, tarfile.ReadError, EOFError) as exc:
            if attempt == retries:
                raise
            LOG.warning("%s: strömmen bröts (%s) — försök %d igen om 30 s", day, exc, attempt + 2)
            time.sleep(30)

    manifest = {"day": day.isoformat(), "status": "done", "variant": variant, "mib": mib,
                "urls": urls, "total_bytes": sum(sizes), "skipped_bytes": skip,
                "bytes": stream.bytes_read, "traces_scanned": scanned,
                "seconds": round(time.monotonic() - t0, 1),
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "aircraft": sorted(found.values(), key=lambda r: r["registration"] or r["hex"])}
    if scanned < 1000:
        # En hel dags dump har tiotusentals spår — färre tyder på trasig/avkortad release
        raise RuntimeError(f"{day}: bara {scanned} spår i dumpen — skriver inget manifest")
    path = day_manifest_file(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(manifest, indent=1))
    tmp.replace(path)
    return manifest


HTTP_DAY = "https://adsb.lol/globe_history/{d:%Y/%m/%d}/traces/"
HTTP_INTERVAL = 1.1


def fetch_day_http(day: date, hexes: set[str]) -> dict:
    """Snabbväg för dagar som finns på adsb.lol:s HTTP-arkiv (uppmätt ~jan 2024–sep 2025).

    ~45 små anrop i stället för en dump på 2–3,7 GB. Dagkatalogen hämtas FÖRST:
    saknas den (404) skrivs inget manifest — då betyder 404 för ett plan "dagen
    saknas", inte "planet flög inte". Finns katalogen betyder 404 per plan att
    planet inte sågs. Ingen flottupptäckt: bara de hex som skickas in."""
    done = day_manifest(day)
    if done:
        return done
    base = HTTP_DAY.format(d=day)
    try:
        http_get(base, min_interval=HTTP_INTERVAL)
    except NotFound:
        return {"day": day.isoformat(), "status": "no_http_day"}
    t0 = time.monotonic()
    found: dict[str, dict] = {}
    for hx in sorted(h.lower() for h in hexes):
        try:
            raw = http_get(f"{base}{hx[-2:]}/trace_full_{hx}.json", min_interval=HTTP_INTERVAL)
        except NotFound:
            continue
        hit = _cargojet_match(raw, known=True)
        dest = trace_file(hx, day)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(gzip.compress(raw))
        found[hx] = hit
    manifest = {"day": day.isoformat(), "status": "done", "source": "http", "variant": "http",
                "hexes_requested": len(hexes), "traces_scanned": None,
                "seconds": round(time.monotonic() - t0, 1),
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "aircraft": sorted(found.values(), key=lambda r: r["registration"] or r["hex"])}
    path = day_manifest_file(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(manifest, indent=1))
    tmp.replace(path)
    return manifest


def _extract_job(args) -> dict:
    day, hexes = args
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        return extract_day(day, hexes)
    except Exception as exc:  # noqa: BLE001 — en dag får inte fälla körningen
        return {"day": day.isoformat(), "status": "error", "error": repr(exc)}


def extract_days(days: list[date], fleet_hexes: set[str], workers: int = 3) -> list[dict]:
    """Hämta alla ohämtade dagar, `workers` dumpar parallellt. Avbrytbar/återupptagbar."""
    todo = [d for d in days if not is_day_done(d)]
    LOG.info("ADS-B-dumpar: %d av %d dagar kvar (%d parallellt)", len(todo), len(days), workers)
    results = []
    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_extract_job, (d, set(fleet_hexes))): d for d in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            per_day = (time.monotonic() - t0) / i
            LOG.info("%s %s: %s plan, %s spår, %.0f MiB, %ss  [%d/%d, ~%.1f h kvar]",
                     r["day"], r.get("status"), len(r.get("aircraft", [])), r.get("traces_scanned"),
                     (r.get("bytes") or 0) / 2**20, r.get("seconds"), i, len(todo),
                     per_day * (len(todo) - i) / 3600)
            if r.get("status") == "error":
                LOG.warning("  fel: %s", r.get("error"))
    return results


# ---------- Täckning och flotta ur manifesten ----------

def day_health(days: list[date]) -> pd.DataFrame:
    rows = []
    for d in days:
        m = day_manifest(d)
        if not m:
            rows.append({"day": d, "done": False})
            continue
        ac = m["aircraft"]
        rows.append({"day": d, "done": True, "variant": m["variant"], "mib": m["mib"],
                     "traces_scanned": m["traces_scanned"],
                     "cargojet_aircraft": len(ac),
                     "with_cjt_callsign": sum(bool(a["cjt_callsigns"]) for a in ac)})
    return pd.DataFrame(rows)


def discovered_aircraft(days: list[date]) -> pd.DataFrame:
    """Alla hex som matchat någon dag, med första/sista dag och hur de matchade."""
    rows = []
    for d in days:
        m = day_manifest(d)
        for a in (m or {}).get("aircraft", []):
            rows.append({"day": d, **{k: a[k] for k in ("hex", "registration", "icao_type", "ownOp")},
                         "cjt": bool(a["cjt_callsigns"]), "by": "+".join(a["matched_by"])})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.groupby("hex").agg(
        registration=("registration", "last"), icao_type=("icao_type", "last"),
        ownOp=("ownOp", "last"), first_seen=("day", "min"), last_seen=("day", "max"),
        days_seen=("day", "nunique"), days_cjt_callsign=("cjt", "sum"),
        matched_by=("by", lambda s: " ".join(sorted(set(s))))).reset_index()
