"""Facit: ADS-B-blocktimmar mot Cargojets rapporterade, per kvartal.

Mål: avvikelse inom ±5 %. Tre mått visas sida vid sida så att det framgår
VAR en eventuell avvikelse kommer ifrån:
  raw        — summan av alla användbara flygningar (inkl. oklassade rutter)
  day_scaled — raw uppskalad för dagar utan ADS-B-dump (bara MLAT-releaser:
               2025-12-14…17, 2026-05-06) och ohämtade dagar
  measured   — bara flygningar med observerad start OCH landning (undre gräns)

Kalibreringsfaktorn reported/day_scaled redovisas också. Den används i
modellen bara med data före det kvartal som prognosticeras (inget läckage).
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from . import adsb, routes
from .common import PROCESSED, quarter_bounds, quarters_between

VALIDATION_CSV = PROCESSED / "validation_block_hours.csv"
TARGET_PCT = 5.0


def quarter_coverage(quarter: str) -> dict:
    start, end = quarter_bounds(quarter)
    days = [start + timedelta(i) for i in range((end - start).days + 1)]
    done = [d for d in days if adsb.is_day_done(d)]
    return {"days_in_quarter": len(days), "days_with_adsb": len(done),
            "day_coverage": len(done) / len(days)}


def block_hours_table(flights: pd.DataFrame, mda: pd.DataFrame,
                      first: str = "2024Q1", last: str = "2026Q2") -> pd.DataFrame:
    f = routes.usable(flights)
    rows = []
    for q in quarters_between(first, last):
        fq = f[f["quarter"] == q]
        cov = quarter_coverage(q)
        raw = float(fq["block_h"].sum())
        measured = float(fq.loc[fq["quality"].isin(["full", "airborne_obs"]), "block_h"].sum())
        scaled = raw / cov["day_coverage"] if cov["day_coverage"] else np.nan
        rep = mda.loc[mda["quarter"] == q, "block_hours"]
        reported = float(rep.iloc[0]) if len(rep) and pd.notna(rep.iloc[0]) else np.nan
        fleet_rep = mda.loc[mda["quarter"] == q, "fleet_operating"]
        rows.append({
            "quarter": q, "reported_block_hours": reported,
            "adsb_raw": round(raw), "adsb_day_scaled": round(scaled) if pd.notna(scaled) else np.nan,
            "adsb_measured_only": round(measured),
            "dev_raw_pct": 100 * (raw / reported - 1) if reported else np.nan,
            "dev_scaled_pct": 100 * (scaled / reported - 1) if reported else np.nan,
            "within_target": abs(100 * (scaled / reported - 1)) <= TARGET_PCT if reported else None,
            "calibration": reported / scaled if reported and scaled else np.nan,
            "flights": int(len(fq)),
            "share_hours_full": fq.loc[fq["quality"] == "full", "block_h"].sum() / raw if raw else np.nan,
            "share_hours_inferred": fq.loc[fq["quality"] == "inferred", "block_h"].sum() / raw if raw else np.nan,
            "aircraft_seen": int(fq["registration"].nunique()),
            "fleet_reported": float(fleet_rep.iloc[0]) if len(fleet_rep) else np.nan,
            **cov,
        })
    out = pd.DataFrame(rows)
    VALIDATION_CSV.parent.mkdir(parents=True, exist_ok=True)
    out.round(4).to_csv(VALIDATION_CSV, index=False)
    return out


def summary(table: pd.DataFrame) -> str:
    t = table.dropna(subset=["reported_block_hours"])
    t = t[t["day_coverage"] > 0]
    if t.empty:
        return "Ingen överlappning mellan ADS-B och rapporterade kvartal ännu."
    hits = int(t["within_target"].sum())
    mae = t["dev_scaled_pct"].abs().mean()
    return (f"{hits}/{len(t)} kvartal inom ±{TARGET_PCT:.0f} % (dagjusterat); "
            f"medel |avvikelse| {mae:.1f} %, spann {t['dev_scaled_pct'].min():+.1f} … "
            f"{t['dev_scaled_pct'].max():+.1f} %")
