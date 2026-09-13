"""Gemensam grund för CJT-nowcasten: sökvägar, diskcachad HTTP och kvartal.

Reproducerbarhetsprincipen: varje externt svar sparas EXAKT som det kom under
data/raw/ och hämtas aldrig igen (om inte --refresh). Allt i data/processed/
är härlett ur raw/ + manual/ och kan byggas om offline. manual/ är incheckat
och innehåller det som inte går att skrapa (konsensus, klassregler,
överstyrningar) — med källa per rad.

Varje hämtning loggas i data/raw/_manifest.jsonl (url, fil, sha256, tid) så
att det i efterhand går att se exakt vilken version av en källa en körning
byggde på.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
MANUAL = ROOT / "manual"
MANIFEST = RAW / "_manifest.jsonl"

LOG = logging.getLogger("cjt")


def _load_dotenv() -> None:
    """KEY=VALUE ur projektrotens .env (samma regler som atlas_choke.config):
    redan satta miljövariabler vinner. Nycklar (STOOQ_API_KEY) läggs där, aldrig i koden."""
    try:
        lines = (ROOT.parent / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

_SECRET_PARAMS = re.compile(r"((?:apikey|api_key|token|key)=)[^&]+", re.IGNORECASE)


def redact(url: str) -> str:
    """Maskera nycklar i URL:er innan de skrivs till manifest eller logg."""
    return _SECRET_PARAMS.sub(r"\1<redacted>", url)

UA = "atlas-cjt-nowcast/0.1 (research; polite, cached)"
# Yahoo/cargojet.com strypter icke-browser-UA — samma headers som atlas_choke.sources.markets
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HTTP_TIMEOUT = float(os.environ.get("CJT_HTTP_TIMEOUT", "60"))
# Samma medvetna val som atlas_choke.config: publik läsdata, tolerant TLS som
# fallback när maskinens CA-kedja avvisas av strikt OpenSSL 3.x.
_LENIENT_TLS = ssl._create_unverified_context()

_manifest_lock = threading.Lock()
_pace_lock = threading.Lock()
_last_call: dict[str, float] = {}


class NotFound(Exception):
    """Källan svarade 404 — cachas som negativt svar så vi inte frågar igen."""


def _pace(host: str, min_interval: float) -> None:
    """Global takthållning per värd (gratis community-tjänster stryper annars)."""
    if min_interval <= 0:
        return
    with _pace_lock:
        wait = min_interval - (time.monotonic() - _last_call.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()


def _log_manifest(url: str, path: Path, payload: bytes | None, status: int) -> None:
    entry = {
        "url": redact(url),
        "path": str(path.relative_to(RAW)) if path.is_relative_to(RAW) else str(path),
        "status": status,
        "sha256": hashlib.sha256(payload).hexdigest() if payload is not None else None,
        "bytes": len(payload) if payload is not None else 0,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with _manifest_lock:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        with MANIFEST.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def http_get(url: str, *, headers: dict[str, str] | None = None,
             min_interval: float = 0.0, retries: int = 4) -> bytes:
    """GET med gzip-avkodning, backoff på 429/5xx/nätfel. 404 → NotFound."""
    host = urllib.parse.urlsplit(url).netloc
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Encoding", "gzip")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    delay = 5.0
    for attempt in range(retries + 1):
        _pace(host, min_interval)
        try:
            try:
                resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
            except urllib.error.URLError as exc:
                if "CERTIFICATE_VERIFY_FAILED" not in str(getattr(exc, "reason", "")):
                    raise
                resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_LENIENT_TLS)
            with resp:
                raw = resp.read()
                # Lita på innehållet, inte headern: adsb.lol:s katalogsidor kommer som
                # okomprimerad HTML trots "Content-Encoding: gzip" (uppmätt 2026-09-13).
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NotFound(url) from exc
            if exc.code not in (403, 429, 500, 502, 503, 504) or attempt == retries:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == retries:
                raise
            wait = delay
            LOG.debug("nätfel %s: %s", url, exc)
        LOG.info("backoff %.0fs för %s (försök %d)", wait, host, attempt + 1)
        LOG.debug("  url: %s", redact(url))
        time.sleep(wait)
        delay = min(delay * 2, 300.0)
    raise RuntimeError("unreachable")


def fetch_cached(url: str, dest: Path, *, headers: dict[str, str] | None = None,
                 min_interval: float = 0.0, refresh: bool = False,
                 allow_404: bool = False, gzip_store: bool = False) -> Path | None:
    """Hämta url till dest en gång. Returnerar dest, eller None vid (cachad) 404.

    404 sparas som en tom `<dest>.404`-markör när allow_404 är satt — för
    ADS-B-spår betyder 404 "planet sågs inte den dagen", vilket är ett
    giltigt och reproducerbart svar som inte ska frågas om igen.

    gzip_store: komprimera på disk (dest bör sluta på .gz). ~42k ADS-B-spår
    à 0,1–0,7 MB okomprimerat blir annars ~15 GB. sha256 i manifestet gäller
    alltid det okomprimerade svaret.
    """
    miss = dest.with_name(dest.name + ".404")
    if not refresh:
        if dest.exists():
            return dest
        if allow_404 and miss.exists():
            return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = http_get(url, headers=headers, min_interval=min_interval)
    except NotFound:
        if not allow_404:
            raise
        miss.touch()
        _log_manifest(url, miss, None, 404)
        return None
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(gzip.compress(payload, compresslevel=6) if gzip_store else payload)
    tmp.replace(dest)
    _log_manifest(url, dest, payload, 200)
    return dest


# ---------- Kvartal ----------

def quarter_label(d: date | datetime) -> str:
    """2024-05-03 → '2024Q2'."""
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarter_bounds(label: str) -> tuple[date, date]:
    """'2024Q2' → (2024-04-01, 2024-06-30) inklusive."""
    year, q = int(label[:4]), int(label[-1])
    start = date(year, 3 * (q - 1) + 1, 1)
    end_month = 3 * q
    end = (date(year + 1, 1, 1) if end_month == 12 else date(year, end_month + 1, 1))
    return start, date.fromordinal(end.toordinal() - 1)


def shift_quarter(label: str, n: int) -> str:
    """shift_quarter('2025Q1', -4) → '2024Q1'."""
    idx = int(label[:4]) * 4 + int(label[-1]) - 1 + n
    return f"{idx // 4}Q{idx % 4 + 1}"


def quarters_between(first: str, last: str) -> list[str]:
    out, q = [], first
    while q <= last:
        out.append(q)
        q = shift_quarter(q, 1)
    return out
