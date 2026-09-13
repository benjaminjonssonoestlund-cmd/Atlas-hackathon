"""Cargojet tracker — blocktimmar innevarande kvartal mot rapporterat.

MÄTNING: luftburna intervall per plan ur ADS-B-spåren (cjt_tracker.merge_intervals)
per Toronto-dygn, plus TAXI_H_PER_FLIGHT per flygning → blocktimmar enligt
MD&A:s definition (bromsarna släpps → sätts). Uppmätt 2026-09-13 i
cjt_nowcast/flights: median blocktid − luftburen tid för fullt observerade
taxikedjor ≈ 0,24 h.

LEDGER: varje dygns mätvärden sparas på disk (data/cargojet/block_hours.json)
och uppdateras var 15:e minut, även dygnet som pågår. Ett dygns värde sänks
aldrig: live-inspelningen rensas efter 4 dygn och spårfiler kan fattas
tillfälligt, men timmar som en gång setts ligger kvar.

KALIBRERING: community-ADS-B missar flygningar (hav, Latinamerika, osedda
rotationer). Faktorn = median(rapporterat / ADS-B-skattat) över tidigare
kvartal där dagsdumparna täcker minst MIN_CAL_DAYS kompletta dygn. ADS-B-
skattningen veckodagsbalanseras upp till hela kvartalet. Bara kvartal före
det innevarande används.

JÄMFÖRELSE: samma kvartal 1–2 år bakåt ur Cargojets MD&A
(cjt_nowcast/data/processed/mda_quarterly.csv). Rapporterna har bara
kvartalssummor, så "samma tidpunkt" = kvartalssumman × andelen av kvartalet
som gått (pro rata). Hela kvartalets siffra redovisas bredvid.
"""

from __future__ import annotations

import csv
import json
import statistics
import threading
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable

from . import cjt_tracker as T
from .cjt_tracker import ET

MDA_CSV = T.NOWCAST_PROCESSED / "mda_quarterly.csv"
LEDGER_PATH = Path(__file__).resolve().parent.parent / "data" / "cargojet" / "block_hours.json"

TAXI_H_PER_FLIGHT = 0.24
MIN_CAL_DAYS = 7
CAL_QUARTERS = 12
FINAL_AFTER_DAYS = 4          # efter publiceringsfördröjning + live-inspelningens 4 dygn
# Om MD&A-tabellen saknas: "Block hours" i Cargojets MD&A Q3 2024 och Q3 2025.
REPORTED_FALLBACK = {"2024Q3": 18928.0, "2025Q3": 15861.0}

IntervalsFn = Callable[[datetime, datetime], dict[str, list[list]]]
DayFn = Callable[[date], bool]


# ---------- Kvartal och dygn ----------

def quarter_end(year: int, q: int) -> datetime:
    return T.quarter_start(year + 1, 1) if q == 4 else T.quarter_start(year, q + 1)


def et_days(start: datetime, end: datetime) -> list[date]:
    first, last = start.astimezone(ET).date(), end.astimezone(ET).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def previous_quarters(year: int, q: int, n: int) -> list[tuple[int, int]]:
    out = []
    for _ in range(n):
        year, q = (year, q - 1) if q > 1 else (year - 1, 4)
        out.append((year, q))
    return out


# ---------- Rapporterat ----------

def load_reported(path: Path = MDA_CSV) -> dict[str, float]:
    """Rapporterade blocktimmar per kvartal ("2025Q3" → 15861)."""
    out = dict(REPORTED_FALLBACK)
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    out[r["quarter"]] = float(r["block_hours"])
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    return out


# ---------- Mätning ----------

def day_stats(intervals_by_hex: dict[str, list[list]], start: datetime, end: datetime) -> dict[date, dict]:
    """Luftburna timmar, flygningar (räknade på startdygnet) och plan per Toronto-dygn."""
    hours, craft = T.hours_by_day(intervals_by_hex, start, end)
    flights: dict[date, int] = defaultdict(int)
    s, e = start.timestamp(), end.timestamp()
    for intervals in intervals_by_hex.values():
        for t0, t1, _ in T.merge_intervals(intervals):
            if t1 > s and t0 < e:
                flights[datetime.fromtimestamp(max(t0, s), ET).date()] += 1
    return {d: {"airborne_h": round(hours.get(d, 0.0), 3), "flights": flights.get(d, 0),
                "aircraft": len(craft.get(d, ()))}
            for d in set(hours) | set(flights)}


def block_h(stats: dict) -> float:
    return stats["airborne_h"] + TAXI_H_PER_FLIGHT * stats["flights"]


def estimate_total(measured: dict[date, float], days: list[date],
                   reference: dict[date, float], last_fraction: float = 1.0) -> dict:
    """Uppmätta dygn som de är; övriga med samma veckodags snitt i `reference`
    (sista dygnet viktat med `last_fraction`). Utan referens skattas inget."""
    by_wd: dict[int, list[float]] = defaultdict(list)
    for d, v in reference.items():
        by_wd[d.weekday()].append(v)
    mean_all = sum(reference.values()) / len(reference) if reference else None

    def typical(d: date) -> float | None:
        vals = by_wd.get(d.weekday())
        return sum(vals) / len(vals) if vals else mean_all

    meas = est = 0.0
    for i, d in enumerate(days):
        if d in measured:
            meas += measured[d]
        elif typical(d) is not None:
            est += typical(d) * (max(0.0, min(1.0, last_fraction)) if i == len(days) - 1 else 1.0)
    return {"total": meas + est, "measured": meas, "estimated": est}


# ---------- Ledger ----------

class Ledger:
    """Dygnsvärden på disk. Ett dygns blocktimmar sänks aldrig."""

    def __init__(self, path: Path = LEDGER_PATH) -> None:
        self.path = path
        self.lock = threading.Lock()
        try:
            self.doc = json.loads(path.read_text())
        except (OSError, ValueError):
            self.doc = {}
        self.doc.setdefault("days", {})

    def merge_day(self, d: date, stats: dict, complete: bool, final: bool, now_ts: float) -> None:
        key = d.isoformat()
        old = self.doc["days"].get(key)
        rec = dict(old) if old else {"airborne_h": 0.0, "flights": 0, "aircraft": 0}
        if old is None or block_h(stats) > block_h(old):
            rec.update(airborne_h=stats["airborne_h"], flights=stats["flights"],
                       aircraft=max(stats["aircraft"], rec["aircraft"]), updated_at=int(now_ts))
        rec["block_h"] = round(block_h(rec), 2)
        rec["complete"] = bool(rec.get("complete")) or complete
        rec["final"] = bool(rec.get("final")) or final
        self.doc["days"][key] = rec

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(json.dumps(self.doc, indent=1, sort_keys=True))
        tmp.replace(self.path)


def update_ledger(ledger: Ledger, now: datetime, intervals_for_window: IntervalsFn,
                  complete_day: DayFn) -> None:
    """Mät innevarande kvartal hittills (inkl. pågående dygn) och spara."""
    now = now.astimezone(ET)
    year, q = T.quarter_of(now.date())
    start = T.quarter_start(year, q)
    stats = day_stats(intervals_for_window(start, now), start, now)
    empty = {"airborne_h": 0.0, "flights": 0, "aircraft": 0}
    with ledger.lock:
        for d in et_days(start, now):
            complete = d < now.date() and complete_day(d)
            final = complete and (now.date() - d).days >= FINAL_AFTER_DAYS
            ledger.merge_day(d, stats.get(d, empty), complete, final, now.timestamp())
        ledger.doc["updated_at"] = int(now.timestamp())
        ledger.save()


# ---------- Kalibrering ----------

def adsb_quarter_estimate(intervals_for_window: IntervalsFn, complete_day: DayFn,
                          year: int, q: int) -> dict | None:
    start, end = T.quarter_start(year, q), quarter_end(year, q)
    days = et_days(start, end - timedelta(seconds=1))
    ok = [d for d in days if complete_day(d)]
    if len(ok) < MIN_CAL_DAYS:
        return None
    stats = day_stats(intervals_for_window(start, end), start, end)
    measured = {d: block_h(stats[d]) for d in ok
                if d in stats and stats[d]["aircraft"] >= T.MIN_AIRCRAFT_PER_DAY}
    if len(measured) < MIN_CAL_DAYS:
        return None
    return {**estimate_total(measured, days, measured), "days_used": len(measured)}


def calibration_factor(reported: dict[str, float], adsb_totals: dict[str, float]) -> dict:
    ratios = {k: reported[k] / v for k, v in adsb_totals.items() if reported.get(k) and v}
    return {"factor": round(statistics.median(ratios.values()), 4) if ratios else None,
            "ratios": {k: round(r, 4) for k, r in sorted(ratios.items())}}


def compute_calibration(now: datetime, intervals_for_window: IntervalsFn, complete_day: DayFn,
                        reported: dict[str, float]) -> dict:
    year, q = T.quarter_of(now.astimezone(ET).date())
    per_q = {}
    for y, qq in previous_quarters(year, q, CAL_QUARTERS):
        label = f"{y}Q{qq}"
        if reported.get(label):
            est = adsb_quarter_estimate(intervals_for_window, complete_day, y, qq)
            if est:
                per_q[label] = est
    cal = calibration_factor(reported, {k: v["total"] for k, v in per_q.items()})
    cal["adsb_block_hours"] = {k: round(v["total"]) for k, v in per_q.items()}
    cal["days_used"] = {k: v["days_used"] for k, v in per_q.items()}
    cal["for_quarter"] = f"{year}Q{q}"
    cal["computed_at"] = int(time.time())
    return cal


# ---------- Resultat för UI:t ----------

def quarter_to_date(now: datetime, ledger_doc: dict, reported: dict[str, float],
                    calibration: dict | None) -> dict:
    """Blocktimmar innevarande kvartal hittills mot rapporterat samma tidpunkt 1–2 år bakåt."""
    now = now.astimezone(ET)
    year, q = T.quarter_of(now.date())
    start, end = T.quarter_start(year, q), quarter_end(year, q)
    days = et_days(start, now)
    today = now.date()

    measured: dict[date, float] = {}
    reference: dict[date, float] = {}
    for d in days:
        rec = ledger_doc.get("days", {}).get(d.isoformat())
        if not rec or rec["aircraft"] < T.MIN_AIRCRAFT_PER_DAY:
            continue
        if d == today:
            measured[d] = block_h(rec)                 # pågående dygn: uppmätt hittills
        elif rec.get("complete"):
            measured[d] = reference[d] = block_h(rec)
    day_frac = (now - datetime.combine(today, dtime(0), tzinfo=ET)).total_seconds() / 86400
    est = estimate_total(measured, days, reference, day_frac)

    factor = (calibration or {}).get("factor")
    hours = round(est["total"] * (factor or 1.0)) if reference or measured else None
    elapsed = (now - start) / (end - start)

    comparisons = []
    for k in range(1, T.YEARS_BACK + 1):
        full = reported.get(f"{year - k}Q{q}")
        comparisons.append({
            "quarter": f"Q{q}", "year": str(year - k),
            "hours": round(full * elapsed) if full else None,
            "reported_quarter_hours": full,
            "source": f"Cargojet MD&A Q{q} {year - k}, pro rata {elapsed:.1%} av kvartalet",
        })
    ref = comparisons[0]["hours"] if comparisons else None
    level = None if hours is None or ref is None else ("HIGH" if hours >= ref else "LOW")

    return {
        "quarter": f"Q{q}", "year": str(year),
        "hours": hours,
        "level": level,
        "level_basis": f"Q{q} {year} hittills mot Q{q} {year - 1} samma tidpunkt",
        "comparisons": comparisons,
        "start": start.isoformat(), "end": now.isoformat(),
        "start_ts": int(start.timestamp()), "end_ts": int(now.timestamp()),
        "quarter_end_ts": int(end.timestamp()),
        "elapsed_fraction": round(elapsed, 4),
        "pace_quarter_hours": round(hours / elapsed) if hours and elapsed > 0 else None,
        "adsb_block_hours": round(est["total"], 1),
        "measured_block_hours": round(est["measured"], 1),
        "estimated_block_hours": round(est["estimated"], 1),
        "measured_hours": round(est["measured"] * (factor or 1.0)),     # kalibrerat, summerar till hours
        "estimated_hours": round(est["estimated"] * (factor or 1.0)),
        "days": len(days),
        "covered_days": len(measured),
        "coverage": round(len(measured) / len(days), 3),
        "calibration": calibration,
        "ledger_updated_at": ledger_doc.get("updated_at"),
        "unit": "blocktimmar",
        "method": ("ADS-B: luftburen tid + taxi per flygning, per dygn i ledger (sänks aldrig); "
                   "dygn utan data skattas med samma veckodags snitt; × kalibrering mot rapporterade "
                   "kvartal. Jämförelse: MD&A-kvartalssumma pro rata."),
    }
