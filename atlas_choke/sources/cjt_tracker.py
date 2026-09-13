"""Cargojet tracker — kvartalsdata för UI:t.

HISTORIK: backtesten i cjt_nowcast (data/processed/surprise_vs_consensus.csv,
2024Q2–2026Q2). Per kvartal: konsensus för omsättning, modellens riktning mot
konsensus (ÖVER/UNDER → HIGHER/LOWER), rapporterat utfall och om riktningen
var rätt. Kvartal utan facit (pågående nowcast) tas inte med.

FLYGTIMMAR HITTILLS: luftburna intervall per plan ur ADS-B-spåren (samma källa
som globen). För innevarande kvartal och samma kvartal 1–2 år bakåt summeras
flygtid i fönstret [kvartalsstart, samma datum och klockslag] i Toronto-tid.
Dygn där data saknas (färre än MIN_AIRCRAFT_PER_DAY plan med flygningar —
Cargojet flyger alltid fler) skattas med snittet för samma veckodag bland
täckta dygn i fönstret. Täckningen redovisas per fönster.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/Toronto")
NOWCAST_PROCESSED = Path(__file__).resolve().parent.parent.parent / "cjt_nowcast" / "data" / "processed"
SURPRISE_CSV = NOWCAST_PROCESSED / "surprise_vs_consensus.csv"

REVENUE_METRIC = "Omsättning"
DIRECTION_EN = {"ÖVER": "HIGHER", "UNDER": "LOWER"}
YEARS_BACK = 2

MIN_AIRCRAFT_PER_DAY = 5
MERGE_GAP_S = 10 * 60          # dagsgränser och dubbletter mellan källor
MIN_FLIGHT_S = 15 * 60
MIN_MAX_ALT_FT = 5000


# ---------- Kvartal ----------

def quarter_of(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def quarter_start(year: int, q: int) -> datetime:
    return datetime(year, 3 * (q - 1) + 1, 1, tzinfo=ET)


def same_moment_year(dt: datetime, year: int) -> datetime:
    try:
        return dt.replace(year=year)
    except ValueError:            # 29 feb
        return dt.replace(year=year, day=28)


# ---------- Historik ur backtesten ----------

def load_history(path: Path = SURPRISE_CSV) -> list[dict]:
    """Backtestens kvartal med facit, nyast först."""
    items = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["mått"] != REVENUE_METRIC or r["rätt"] not in ("True", "False"):
                continue
            q = r["kvartal"]                                    # "2024Q2"
            items.append({
                "id": q,
                "quarter": q[4:],
                "year": q[:4],
                "metric": "REVENUE",
                "consensus_mcad": float(r["konsensus"]),
                "forecast_mcad": float(r["prognos"]),
                "signal": DIRECTION_EN.get(r["prognos_vs_konsensus"], "NO SIGNAL"),
                "result_mcad": float(r["utfall"]),
                "result_vs_consensus": DIRECTION_EN.get(r["utfall_vs_konsensus"], "NO SIGNAL"),
                "correct": r["rätt"] == "True",
                "report_date": r["rapportdag"] or None,
            })
    items.sort(key=lambda it: it["id"], reverse=True)
    return items


# ---------- Flygtimmar ----------

def merge_intervals(intervals: list[list]) -> list[list]:
    """Slå ihop överlappande/angränsande [t0, t1, max_alt] och släng taxning/brus."""
    merged: list[list] = []
    for t0, t1, alt in sorted(intervals):
        if merged and t0 - merged[-1][1] <= MERGE_GAP_S:
            merged[-1][1] = max(merged[-1][1], t1)
            merged[-1][2] = max(merged[-1][2], alt)
        else:
            merged.append([t0, t1, alt])
    return [m for m in merged if m[1] - m[0] >= MIN_FLIGHT_S and m[2] >= MIN_MAX_ALT_FT]


def _midnight_after(ts: float) -> float:
    d = datetime.fromtimestamp(ts, ET).date() + timedelta(days=1)
    return datetime.combine(d, dtime(0), tzinfo=ET).timestamp()


def hours_by_day(intervals_by_hex: dict[str, list[list]], start: datetime,
                 end: datetime) -> tuple[dict[date, float], dict[date, set]]:
    """Flygtimmar och flygande plan per Toronto-dygn inom [start, end]."""
    hours: dict[date, float] = defaultdict(float)
    craft: dict[date, set] = defaultdict(set)
    s, e = start.timestamp(), end.timestamp()
    for hexid, intervals in intervals_by_hex.items():
        for t0, t1, _ in merge_intervals(intervals):
            cur, stop = max(t0, s), min(t1, e)
            while cur < stop:
                piece_end = min(stop, _midnight_after(cur))
                d = datetime.fromtimestamp(cur, ET).date()
                hours[d] += (piece_end - cur) / 3600
                craft[d].add(hexid)
                cur = piece_end
    return hours, craft


def window_estimate(intervals_by_hex: dict[str, list[list]], start: datetime, end: datetime) -> dict:
    """Uppmätta + skattade flygtimmar i fönstret, med täckning."""
    hours, craft = hours_by_day(intervals_by_hex, start, end)
    days = [start.date() + timedelta(days=i) for i in range((end.date() - start.date()).days + 1)]
    covered = {d for d in days if len(craft.get(d, ())) >= MIN_AIRCRAFT_PER_DAY}
    last = days[-1]
    complete = days[:-1]

    by_weekday: dict[int, list[float]] = defaultdict(list)
    for d in complete:
        if d in covered:
            by_weekday[d.weekday()].append(hours[d])
    covered_complete = [hours[d] for d in complete if d in covered]
    fallback = sum(covered_complete) / len(covered_complete) if covered_complete else None

    def typical(d: date) -> float | None:
        vals = by_weekday.get(d.weekday())
        return sum(vals) / len(vals) if vals else fallback

    measured = sum(hours[d] for d in days if d in covered)
    estimated = 0.0
    for d in complete:
        if d not in covered and typical(d) is not None:
            estimated += typical(d)
    if last not in covered and typical(last) is not None:
        day_frac = (end - datetime.combine(last, dtime(0), tzinfo=ET)).total_seconds() / 86400
        estimated += typical(last) * max(0.0, min(1.0, day_frac))

    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "start_ts": int(start.timestamp()), "end_ts": int(end.timestamp()),
        "hours": round(measured + estimated, 1) if covered else None,
        "measured_hours": round(measured, 1),
        "estimated_hours": round(estimated, 1),
        "days": len(days),
        "covered_days": len(covered),
        "coverage": round(len(covered) / len(days), 3),
    }


def quarter_to_date(now: datetime,
                    intervals_for_window: Callable[[datetime, datetime], dict[str, list[list]]]) -> dict:
    """Innevarande kvartal hittills mot samma fönster 1–2 år bakåt."""
    now = now.astimezone(ET)
    year, q = quarter_of(now.date())
    start = quarter_start(year, q)
    current = window_estimate(intervals_for_window(start, now), start, now)
    comparisons = []
    for k in range(1, YEARS_BACK + 1):
        s, e = start.replace(year=year - k), same_moment_year(now, year - k)
        comparisons.append({"quarter": f"Q{q}", "year": str(year - k),
                            **window_estimate(intervals_for_window(s, e), s, e)})
    ref = comparisons[0]["hours"] if comparisons else None
    level = None if current["hours"] is None or ref is None else \
        ("HIGH" if current["hours"] >= ref else "LOW")
    return {
        "quarter": f"Q{q}", "year": str(year), **current,
        "level": level,
        "level_basis": f"Q{q} {year} hittills mot Q{q} {year - 1} samma period",
        "comparisons": comparisons,
        "unit": "luftburna flygtimmar ur ADS-B",
        "method": "uppmätt per dygn; dygn utan data skattas med samma veckodags snitt i fönstret",
    }
