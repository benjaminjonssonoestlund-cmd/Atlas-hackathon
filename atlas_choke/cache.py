"""Persistent cachelager med stale-while-revalidate, circuit breaker och hälsa.

    Live Source → Fetch Layer → Persistent Cache → Freshness Policy → Application

Motorn är GENERELL: den känner inte till en enda datakälla. All källspecifik
metadata (ttl, stale_after, refresh_policy, priority, warm_on_start,
retry_policy, fallback_policy) ligger som DATA i data/source_policies.json och
slås upp via nyckelns prefix. Ny källa = ny rad i JSON, ingen kodändring.

Publikt API är oförändrat — `get`, `set`, `get_or_fetch(key, ttl, fetch)` —
så alla ~96 befintliga anropsplatser byter beteende utan att röras.

Färskhetsnivåer:
  ålder < ttl            → FÄRSK: returneras direkt
  ttl ≤ ålder < stale    → GAMMAL MEN GILTIG: returneras DIREKT + uppdateras i
                           bakgrunden (stale-while-revalidate). Ingen väntan.
  ålder ≥ stale_after    → FÖR GAMMAL: hämtas synkront; misslyckas det serveras
                           ändå det gamla värdet (fallback_policy).

En trasig källa får aldrig fälla systemet: efter N fel öppnas en circuit breaker
med exponentiell backoff och Atlas serverar cache tills källan repar sig.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import sqlite3
import statistics
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

DATA = Path(__file__).resolve().parent / "data"
POLICY_FILE = DATA / "source_policies.json"
# Sökvägen är överstyrbar så tester (och parallella instanser) aldrig skriver i
# den skarpa cachen — pytest gjorde just det innan ATLAS_CACHE_DB fanns.
DB_FILE = Path(os.environ.get("ATLAS_CACHE_DB") or (DATA / "cache.db"))

LOG = logging.getLogger("atlas.cache")

# Hur länge en källa utan lyckad hämtning räknas som offline i hälsovyn.
OFFLINE_AFTER_FAILURES = 3


# ---------- Policy (ren datauppslagning) ----------

class Policies:
    """Läser policy-JSON och slår upp per nyckelprefix. Ingen domänlogik."""

    def __init__(self, path: Path = POLICY_FILE) -> None:
        self._path = path
        self._raw: dict = {}
        self._cache: dict[str, dict] = {}
        self.reload()

    def reload(self) -> None:
        try:
            self._raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — utan fil gäller inbyggda försiktiga värden
            LOG.warning("kunde inte läsa %s (%s) — använder default", self._path.name, exc)
            self._raw = {}
        self._cache.clear()

    @staticmethod
    def source_of(key: str) -> str:
        """Nyckelns källa = allt före första kolon ('fred:series:VIXCLS' → 'fred')."""
        return key.split(":", 1)[0] if ":" in key else key

    def for_key(self, key: str) -> dict:
        src = self.source_of(key)
        hit = self._cache.get(src)
        if hit is None:
            hit = self._resolve(src)
            self._cache[src] = hit
        return hit

    def _resolve(self, src: str) -> dict:
        classes = self._raw.get("classes") or {}
        default = self._raw.get("default") or {}
        entry = (self._raw.get("sources") or {}).get(src)
        explicit = entry is not None
        entry = dict(entry or default)
        base = dict(classes.get(entry.get("class") or default.get("class") or "", {}))
        base.update({k: v for k, v in entry.items() if k not in ("class", "_doc")})
        base.setdefault("ttl", 1800)
        base.setdefault("stale_after", max(base["ttl"] * 8, 43200))
        base.setdefault("refresh_policy", "background")
        base.setdefault("priority", 5)
        base.setdefault("warm_on_start", False)
        base.setdefault("retry_policy", {"attempts": 2, "backoff_s": 2, "max_backoff_s": 60})
        base.setdefault("fallback_policy", "serve_stale")
        base.setdefault("circuit", {"failures_to_open": 3, "open_s": 300, "max_open_s": 3600})
        # Okänd källa: TTL:n som anropet skickar med får vinna (bakåtkompatibelt).
        base["ttl_is_authoritative"] = explicit
        base.setdefault("label", src)
        return base

    def warm_plan(self) -> list[dict]:
        plan = (self._raw.get("warm_plan") or {}).get("targets") or []
        return sorted([t for t in plan if t.get("path")],
                      key=lambda t: (t.get("priority", 5), t["path"]))


POLICIES = Policies()


# ---------- Hälsa per källa ----------

class SourceHealth:
    """Räknare + circuit breaker för EN källa. Ren aritmetik, inget källspecifikt.

    Tillståndet PERSISTERAS: en trasig källa ska inte återupptäckas vid varje
    omstart. Utan det betalar första anropet efter en omstart hela timeout-
    kostnaden igen (mätt: 207 s mot 9 s med öppen brytare).
    """

    def __init__(self, name: str, policy: dict) -> None:
        self.name = name
        self.label = policy.get("label", name)
        self.policy = policy
        self.hits = 0            # serverat ur cache (färskt)
        self.stale_hits = 0      # serverat gammalt medan uppdatering skedde
        self.misses = 0          # måste hämtas synkront
        self.successes = 0
        self.failures = 0
        self.failures_24h: deque[float] = deque()
        self.consecutive_failures = 0
        self.latencies: deque[float] = deque(maxlen=50)
        self.last_success: float | None = None
        self.last_failure: float | None = None
        self.last_error: str = ""
        self.open_until: float = 0.0
        self.open_rounds = 0
        self._on_change: Callable[[SourceHealth], None] | None = None

    # -- persistens --
    def to_row(self) -> dict:
        return {"consecutive_failures": self.consecutive_failures,
                "open_until": self.open_until, "open_rounds": self.open_rounds,
                "last_success": self.last_success, "last_failure": self.last_failure,
                "last_error": self.last_error, "successes": self.successes,
                "failures": self.failures,
                "failures_24h": list(self.failures_24h)[-500:]}

    def load_row(self, row: dict) -> None:
        self.consecutive_failures = int(row.get("consecutive_failures", 0))
        self.open_until = float(row.get("open_until", 0.0))
        self.open_rounds = int(row.get("open_rounds", 0))
        self.last_success = row.get("last_success")
        self.last_failure = row.get("last_failure")
        self.last_error = row.get("last_error", "")
        self.successes = int(row.get("successes", 0))
        self.failures = int(row.get("failures", 0))
        self.failures_24h = deque(row.get("failures_24h") or [])
        self._prune()

    def _changed(self) -> None:
        if self._on_change:
            try:
                self._on_change(self)
            except Exception:  # noqa: BLE001 — hälsopersistens får aldrig fälla ett anrop
                pass

    # -- circuit breaker --
    def blocked(self) -> bool:
        """True = källan är avstängd just nu (vi hämtar inte, vi serverar cache)."""
        return time.time() < self.open_until

    def record_success(self, elapsed: float) -> None:
        was_failing = self.consecutive_failures or self.open_until
        self.successes += 1
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.open_rounds = 0
        self.latencies.append(elapsed)
        self.last_success = time.time()
        if was_failing:          # tillståndsövergång trasig → frisk
            self._changed()

    def record_failure(self, exc: BaseException) -> None:
        now = time.time()
        self.failures += 1
        self.consecutive_failures += 1
        self.failures_24h.append(now)
        self.last_failure = now
        self.last_error = f"{type(exc).__name__}: {exc}"[:200]
        c = self.policy.get("circuit") or {}
        need = int(c.get("failures_to_open", 3))
        if self.consecutive_failures >= need:
            self.open_rounds += 1
            # exponentiell backoff, kapad
            wait = float(c.get("open_s", 300)) * (2 ** (self.open_rounds - 1))
            self.open_until = now + min(wait, float(c.get("max_open_s", 3600)))
            LOG.warning("circuit öppen för %s i %.0f s (%d fel i rad): %s",
                        self.name, self.open_until - now, self.consecutive_failures,
                        self.last_error)
        self._changed()

    def _prune(self) -> int:
        cutoff = time.time() - 86400
        while self.failures_24h and self.failures_24h[0] < cutoff:
            self.failures_24h.popleft()
        return len(self.failures_24h)

    def status(self, age: float | None) -> str:
        """Healthy | Delayed | Stale | Offline — härlett, inte satt."""
        if self.blocked() or (self.consecutive_failures >= OFFLINE_AFTER_FAILURES
                              and self.last_success is None):
            return "Offline"
        if age is None:
            return "Offline" if self.failures and not self.successes else "Unknown"
        if age >= float(self.policy.get("stale_after", 43200)):
            return "Stale"
        if age >= float(self.policy.get("ttl", 1800)) or self.consecutive_failures > 0:
            return "Delayed"
        return "Healthy"

    def snapshot(self, age: float | None) -> dict:
        total = self.hits + self.stale_hits + self.misses
        return {
            "source": self.name,
            "label": self.label,
            "status": self.status(age),
            "cache_age_s": round(age) if age is not None else None,
            "ttl_s": self.policy.get("ttl"),
            "stale_after_s": self.policy.get("stale_after"),
            "last_success": self.last_success,
            "last_success_age_s": (round(time.time() - self.last_success)
                                   if self.last_success else None),
            "last_failure_age_s": (round(time.time() - self.last_failure)
                                   if self.last_failure else None),
            "errors_24h": self._prune(),
            "consecutive_failures": self.consecutive_failures,
            "avg_response_ms": (round(statistics.mean(self.latencies) * 1000)
                                if self.latencies else None),
            "median_response_ms": (round(statistics.median(self.latencies) * 1000)
                                   if self.latencies else None),
            "cache_hit_ratio": round((self.hits + self.stale_hits) / total, 3) if total else None,
            "requests": total,
            "hits": self.hits, "stale_hits": self.stale_hits, "misses": self.misses,
            "successes": self.successes, "failures": self.failures,
            "circuit_open": self.blocked(),
            "circuit_reopens_in_s": (round(self.open_until - time.time())
                                     if self.blocked() else None),
            "last_error": self.last_error or None,
            "priority": self.policy.get("priority"),
            "warm_on_start": self.policy.get("warm_on_start"),
        }


# ---------- Persistent lagring ----------

class _Store:
    """Två nivåer: minne (snabbt) + SQLite (överlever omstart).

    Värden lagras med pickle för att bevara typer exakt (tupler förblir tupler —
    flera anropsplatser indexerar (datum, värde)-par). Går ett värde inte att
    serialisera lever det vidare i minnet; cachen degraderar, den kraschar inte.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._mem: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        # Slås upp vid anrop, inte vid definition — annars kan tester inte peka
        # om DB_FILE till en temporär fil (och skriver i produktionscachen).
        self._path = path if path is not None else DB_FILE
        self._db: sqlite3.Connection | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self._path), check_same_thread=False)
            self._db.execute("""CREATE TABLE IF NOT EXISTS entries (
                key TEXT PRIMARY KEY, source TEXT, stored_at REAL, blob BLOB)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_src ON entries(source)")
            self._db.execute("""CREATE TABLE IF NOT EXISTS source_health (
                source TEXT PRIMARY KEY, updated REAL, state TEXT)""")
            self._db.commit()
        except Exception as exc:  # noqa: BLE001 — utan disk kör vi i minnet
            LOG.warning("diskcache otillgänglig (%s) — kör bara i minnet", exc)
            self._db = None

    def read(self, key: str) -> tuple[float, Any] | None:
        with self._lock:
            hit = self._mem.get(key)
        if hit is not None:
            return hit
        if self._db is None:
            return None
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT stored_at, blob FROM entries WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            value = pickle.loads(row[1])
            with self._lock:
                self._mem[key] = (row[0], value)
            return (row[0], value)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("kunde inte läsa %s ur diskcache: %s", key, exc)
            return None

    def write(self, key: str, value: Any, stored_at: float | None = None) -> None:
        stored_at = stored_at if stored_at is not None else time.time()
        with self._lock:
            self._mem[key] = (stored_at, value)
        if self._db is None:
            return
        try:
            blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:  # noqa: BLE001 — opicklebart värde lever vidare i minnet
            return
        try:
            with self._lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO entries(key, source, stored_at, blob) "
                    "VALUES (?,?,?,?)",
                    (key, Policies.source_of(key), stored_at, blob))
                self._db.commit()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("kunde inte skriva %s till diskcache: %s", key, exc)

    def drop(self, key: str) -> None:
        with self._lock:
            self._mem.pop(key, None)
            if self._db is not None:
                try:
                    self._db.execute("DELETE FROM entries WHERE key=?", (key,))
                    self._db.commit()
                except Exception:  # noqa: BLE001
                    pass

    def save_health(self, source: str, row: dict) -> None:
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO source_health(source, updated, state) "
                    "VALUES (?,?,?)", (source, time.time(), json.dumps(row)))
                self._db.commit()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("kunde inte spara hälsa för %s: %s", source, exc)

    def load_health(self) -> dict[str, dict]:
        if self._db is None:
            return {}
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT source, state FROM source_health").fetchall()
            return {s: json.loads(st) for s, st in rows}
        except Exception as exc:  # noqa: BLE001
            LOG.debug("kunde inte läsa hälsotillstånd: %s", exc)
            return {}

    def stats(self) -> dict:
        rows, oldest = 0, None
        if self._db is not None:
            try:
                with self._lock:
                    rows, oldest = self._db.execute(
                        "SELECT COUNT(*), MIN(stored_at) FROM entries").fetchone()
            except Exception:  # noqa: BLE001
                pass
        size = self._path.stat().st_size if self._path.exists() else 0
        return {"persisted_entries": rows or 0, "memory_entries": len(self._mem),
                "db_bytes": size, "oldest_entry_age_s":
                    round(time.time() - oldest) if oldest else None,
                "persistent": self._db is not None}

    def newest_per_source(self) -> dict[str, float]:
        out: dict[str, float] = {}
        with self._lock:
            for k, (ts, _) in self._mem.items():
                s = Policies.source_of(k)
                out[s] = max(out.get(s, 0.0), ts)
        if self._db is not None:
            try:
                with self._lock:
                    for src, ts in self._db.execute(
                            "SELECT source, MAX(stored_at) FROM entries GROUP BY source"):
                        out[src] = max(out.get(src, 0.0), ts or 0.0)
            except Exception:  # noqa: BLE001
                pass
        return out


# ---------- Cachen ----------

class PersistentCache:
    def __init__(self) -> None:
        self._store = _Store()
        self._health: dict[str, SourceHealth] = {}
        self._hlock = threading.Lock()
        self._inflight: set[str] = set()
        self._iflock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="atlas-refresh")
        # Kunskapen om trasiga källor överlever omstarten.
        self._saved_health = self._store.load_health()

    # -- hälsa --
    def health(self, source: str) -> SourceHealth:
        with self._hlock:
            h = self._health.get(source)
            if h is None:
                h = SourceHealth(source, POLICIES.for_key(source))
                saved = self._saved_health.get(source)
                if saved:
                    h.load_row(saved)
                    if h.blocked():
                        LOG.info("%s var trasig vid förra körningen — circuit "
                                 "fortsatt öppen %.0f s till", source,
                                 h.open_until - time.time())
                h._on_change = lambda hh: self._store.save_health(hh.name, hh.to_row())
                self._health[source] = h
            return h

    # -- publikt API (oförändrad signatur) --
    def get(self, key: str) -> Any | None:
        """Färskt värde eller None. Bakåtkompatibel semantik."""
        hit = self._store.read(key)
        if hit is None:
            return None
        stored_at, value = hit
        pol = POLICIES.for_key(key)
        if time.time() - stored_at > float(pol["ttl"]):
            return None
        return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        self._store.write(key, value)

    def get_or_fetch(self, key: str, ttl: float, fetch: Callable[[], Any]) -> Any:
        pol = POLICIES.for_key(key)
        # Explicit policy vinner; okänd källa behåller anropets TTL.
        eff_ttl = float(pol["ttl"]) if pol.get("ttl_is_authoritative") else float(ttl)
        stale_after = max(float(pol["stale_after"]), eff_ttl)
        h = self.health(Policies.source_of(key))

        hit = self._store.read(key)
        age = (time.time() - hit[0]) if hit else None

        if hit is not None and age < eff_ttl:
            h.hits += 1
            return hit[1]

        # Gammalt men giltigt → svara direkt, uppdatera i bakgrunden.
        if hit is not None and age < stale_after:
            h.stale_hits += 1
            if pol.get("refresh_policy") != "lazy":
                self._refresh_async(key, fetch, h)
            return hit[1]

        # Inget användbart i cachen. Är källan avstängd serverar vi hellre
        # gammal data än inget alls.
        if h.blocked():
            if hit is not None and pol.get("fallback_policy") == "serve_stale":
                h.stale_hits += 1
                return hit[1]
            return None

        h.misses += 1
        value = self._fetch_now(key, fetch, h, pol)
        if value is None and hit is not None and pol.get("fallback_policy") == "serve_stale":
            return hit[1]     # hämtningen sprack — det gamla är bättre än inget
        return value

    # -- intern hämtning --
    def _fetch_now(self, key: str, fetch: Callable[[], Any], h: SourceHealth,
                   pol: dict) -> Any:
        retry = pol.get("retry_policy") or {}
        attempts = max(1, int(retry.get("attempts", 2)))
        backoff = float(retry.get("backoff_s", 2))
        max_backoff = float(retry.get("max_backoff_s", 60))
        retry_timeouts = bool(retry.get("retry_on_timeout", False))
        last_exc: BaseException | None = None
        for i in range(attempts):
            started = time.time()
            try:
                value = fetch()
                h.record_success(time.time() - started)
                if value is not None:
                    self._store.write(key, value)
                return value
            except Exception as exc:  # noqa: BLE001 — felet ägs av cachelagret
                last_exc = exc
                h.record_failure(exc)
                # En timeout betyder "källan är långsam", inte "tillfälligt glapp".
                # Att försöka igen mångdubblar bara väntetiden för anroparen.
                if isinstance(exc, TimeoutError) and not retry_timeouts:
                    break
                if i < attempts - 1 and not h.blocked():
                    time.sleep(min(backoff * (2 ** i), max_backoff))
        LOG.info("hämtning misslyckades för %s: %s", key, last_exc)
        return None

    def _refresh_async(self, key: str, fetch: Callable[[], Any], h: SourceHealth) -> None:
        """Bakgrundsuppdatering — anroparen väntar aldrig på den."""
        if h.blocked():
            return
        with self._iflock:
            if key in self._inflight:
                return
            self._inflight.add(key)

        def _job() -> None:
            try:
                self._fetch_now(key, fetch, h, POLICIES.for_key(key))
            finally:
                with self._iflock:
                    self._inflight.discard(key)

        try:
            self._pool.submit(_job)
        except Exception:  # noqa: BLE001
            with self._iflock:
                self._inflight.discard(key)

    # -- observerbarhet --
    def dashboard(self) -> dict:
        newest = self._store.newest_per_source()
        now = time.time()
        rows = []
        with self._hlock:
            known = set(self._health) | set(newest)
        for src in sorted(known):
            h = self.health(src)
            ts = newest.get(src)
            rows.append(h.snapshot((now - ts) if ts else None))
        order = {"Offline": 0, "Stale": 1, "Delayed": 2, "Unknown": 3, "Healthy": 4}
        rows.sort(key=lambda r: (order.get(r["status"], 5), -(r["errors_24h"] or 0)))
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        tot_hits = sum((r["hits"] or 0) + (r["stale_hits"] or 0) for r in rows)
        tot_req = sum(r["requests"] or 0 for r in rows)
        lat = [r["median_response_ms"] for r in rows if r["median_response_ms"]]
        return {
            "sources": rows,
            "summary": {
                "counts": counts, "total": len(rows),
                "cache_hit_ratio": round(tot_hits / tot_req, 3) if tot_req else None,
                "requests": tot_req,
                "median_response_ms": round(statistics.median(lat)) if lat else None,
                "circuits_open": sum(1 for r in rows if r["circuit_open"]),
                **self._store.stats(),
            },
        }

    def invalidate(self, key: str) -> None:
        self._store.drop(key)


CACHE = PersistentCache()

# Bakåtkompatibelt namn — äldre importer av TTLCache fortsätter fungera.
TTLCache = PersistentCache
