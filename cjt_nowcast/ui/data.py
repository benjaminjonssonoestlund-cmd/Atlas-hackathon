"""Data till omsättnings-UI:t — samma modell och facit som CLI:t, som JSON.

Allt räknas en gång vid start (modellen skattas om per historiskt kvartal, några
sekunder) och hålls i minnet; /api/refresh räknar om efter ny data.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from .. import backtest, consensus, model
from ..common import PROCESSED, shift_quarter

NOWCAST_Q = "2026Q3"
LEDGER = PROCESSED / "block_hours_daily_{q}.csv"


def _clean(v):
    """numpy/NaN → JSON-vänligt."""
    if v is None:
        return None
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if math.isnan(v) else float(v)
    return v


def tests() -> dict:
    df = backtest.surprise_table(nowcast_q=NOWCAST_Q)
    rows = [{k: _clean(v) for k, v in r.items()} for r in df.to_dict("records")]
    hist = [r for r in rows if r["timmar"] == "rapporterade (MD&A)"]
    scored = [r for r in hist if r["rätt"] is not None]
    return {"rows": rows, "hits": sum(r["rätt"] for r in scored), "scored": len(scored),
            "first": hist[0]["kvartal"] if hist else None, "last": hist[-1]["kvartal"] if hist else None}


def nowcast(q: str = NOWCAST_Q) -> dict:
    """Q3-signalen + allt som behövs för att räkna om prognosen i webbläsaren när reglaget flyttas.

    total(h) = C·(1 + b0 + bh·h + bf·f + bx·x) + K·jet·H·(1+h) + A   (samma som model.forecast_quarter)
    """
    panel = model.load_panel(last=q)
    nc_path = Path(str(backtest.NOWCAST_CSV).format(q=q))
    nc = pd.read_csv(nc_path).iloc[0] if nc_path.exists() else pd.Series(dtype=object)
    measured = nc.get("hours_yoy", np.nan)
    fc = model.forecast_quarter(panel, q, 0.0 if pd.isna(measured) else float(measured))
    fit: model.RevenueFit = fc["_fit"]
    prev = panel.loc[shift_quarter(q, -4)]
    cons = consensus.load().set_index("quarter")
    c = cons.loc[q] if q in cons.index else pd.Series(dtype=object)
    C, H = float(prev["core_rev"]), float(prev["block_hours"])
    K, jet = model.fuel_surcharge_k(panel, before=q), float(panel.at[q, "jet_cad_gal"])
    A = float(fc["amortization_contract_assets"])
    b0, bh = fit.intercept, fit.coef["hours_yoy"]
    f, x = float(fc["fuel_yoy"]), float(fc["fx_yoy"])
    cons_rev = _clean(c.get("consensus_revenue_mcad"))
    breakeven = None
    if cons_rev is not None:
        denom = C * bh + K * jet * H
        breakeven = (cons_rev - A - C * (1 + b0 + fit.coef["fuel_yoy"] * f + fit.coef["fx_yoy"] * x)
                     - K * jet * H) / denom if denom else None
    from .. import adsb
    from ..__main__ import SAMPLE_2026Q3
    lag = pd.Timedelta(days=backtest.LAG_SAME_WEEKDAY)
    days = sorted(d.isoformat() for d in SAMPLE_2026Q3
                  if adsb.is_day_done(d) and adsb.is_day_done((pd.Timestamp(d) - lag).date()))
    return {
        "quarter": q,
        "measured_hours_yoy": _clean(measured),
        "measured_hours_yoy_total": _clean(nc.get("hours_yoy_total", np.nan)),
        "hours_method": nc.get("hours_method"),
        "n_pairs": _clean(nc.get("n_pairs", np.nan)),
        "segments": {s: {"now": _clean(nc.get(f"h_{s}", np.nan)), "prev": _clean(nc.get(f"h_prev_{s}", np.nan)),
                         "yoy": _clean(nc.get(f"yoy_{s}", np.nan))} for s in ("domestic", "acmi", "charter")},
        "params": {"C": C, "H": H, "K": K, "jet": jet, "A": A, "b0": b0, "bh": bh,
                   "bf": fit.coef["fuel_yoy"], "bx": fit.coef["fx_yoy"], "fuel_yoy": f, "fx_yoy": x},
        "model": {"kind": fit.kind, "n": fit.n, "r2": fit.r2_in_sample,
                  "cv_rmse": min(fit.cv_rmse_ols, fit.cv_rmse_ridge)},
        "forecast_total_rev": _clean(fc["total_rev"]),
        "prev_total_rev": _clean(prev["total_rev"]),
        "prev_block_hours": H,
        "consensus_revenue": cons_rev,
        "consensus_is_forward": bool(c.get("is_forward", False)),
        "consensus_note": c.get("notes", ""),
        "report_date": c.get("report_date"),
        "breakeven_hours_yoy": _clean(breakeven),
        "sample_days": days,
    }


def daily(q: str = NOWCAST_Q) -> list[dict]:
    path = Path(str(LEDGER).format(q=q))
    if not path.exists():
        return []
    df = pd.read_csv(path)
    return [{k: _clean(v) for k, v in r.items()} for r in df.to_dict("records")]


def payload() -> dict:
    return {"tests": tests(), "nowcast": nowcast(), "daily": daily()}
