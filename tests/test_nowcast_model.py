"""Modellens aritmetik på syntetisk data — inga filer, inget nätverk."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cjt_nowcast import model
from cjt_nowcast.common import quarters_between


def _panel(n_years: int = 10, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    qs = quarters_between("2016Q1", f"{2016 + n_years - 1}Q4")
    n = len(qs)
    hours = 10_000 * np.cumprod(1 + rng.normal(0.02, 0.04, n))
    jet = 2.0 * np.cumprod(1 + rng.normal(0.0, 0.08, n))
    fx = 1.3 * np.cumprod(1 + rng.normal(0.0, 0.02, n))
    core = np.empty(n)
    core[:4] = 100.0
    for t in range(4, n):   # sann modell: y = 0.01 + 0.9·h + 0.05·fuel + 0.3·fx
        y = (0.01 + 0.9 * (hours[t] / hours[t - 4] - 1) + 0.05 * (jet[t] / jet[t - 4] - 1)
             + 0.3 * (fx[t] / fx[t - 4] - 1) + rng.normal(0, 0.002))
        core[t] = core[t - 4] * (1 + y)
    return pd.DataFrame({"block_hours": hours, "core_rev": core, "jet_usd_gal": jet, "usdcad": fx},
                        index=pd.Index(qs, name="quarter"))


def test_revenue_fit_recovers_true_coefficients():
    fit = model.fit_revenue(_panel())
    assert fit.coef["hours_yoy"] == pytest.approx(0.9, abs=0.08)
    assert fit.coef["fx_yoy"] == pytest.approx(0.3, abs=0.15)
    assert fit.intercept == pytest.approx(0.01, abs=0.01)


def test_fit_uses_only_quarters_before_cutoff():
    fit = model.fit_revenue(_panel(), before="2022Q1")
    assert max(fit.train_quarters) < "2022Q1"


def test_implied_hours_inverts_prediction():
    fit = model.fit_revenue(_panel())
    y = fit.predict(0.07, 0.2, -0.01)
    assert fit.implied_hours_yoy(y, 0.2, -0.01) == pytest.approx(0.07, abs=1e-9)


def test_pilot_cost_step_hits_yoy_for_four_quarters():
    steps = pd.DataFrame([{"quarter_from": "2026Q3", "quarter_to": "", "amount_mcad_per_quarter": 4.5}])
    assert model.cost_step_yoy("2026Q2", steps) == 0
    assert model.cost_step_yoy("2026Q3", steps) == 4.5
    assert model.cost_step_yoy("2027Q2", steps) == 4.5
    assert model.cost_step_yoy("2027Q3", steps) == 0     # i basen året innan också


def test_margin_band_never_uses_tested_quarter_or_later():
    qs = quarters_between("2024Q1", "2026Q2")
    panel = pd.DataFrame({"total_rev": 250.0, "adj_ebitda": 80.0}, index=pd.Index(qs, name="quarter"))
    panel.loc["2025Q3", "adj_ebitda"] = 250.0 * 0.90        # extrem marginal i testkvartalet
    panel.loc["2026Q1", "adj_ebitda"] = 250.0 * 0.05        # och efter det
    band = model.margin_band_before(panel, "2025Q3")
    assert all(q < "2025Q3" for q in band["band_quarters"])
    assert band["band_low"] == pytest.approx(0.32) and band["band_high"] == pytest.approx(0.32)


def test_forecast_training_excludes_tested_quarter():
    panel = _panel()
    fit = model.fit_revenue(panel, before="2023Q3")
    assert "2023Q3" not in fit.train_quarters and max(fit.train_quarters) < "2023Q3"


def test_bridge_calibration_recovers_pass_through():
    rng = np.random.default_rng(3)
    n = 12
    vol, pm = rng.normal(0, 10, n), rng.normal(10, 8, n)
    base = np.full(n, 80.0)
    actual = base + 0.8 * vol + 0.4 * pm - 5.0          # bara 40 % av pris/mix når EBITDA
    hist = pd.DataFrame({"volume": vol, "price_mix": pm, "actual": actual, "base_adj_ebitda": base,
                         "cost_steps": 0.0, "residual": actual - (base + vol + pm)})
    cal = model.fit_bridge(hist)
    assert (cal.a_volume, cal.b_price_mix, cal.c) == pytest.approx((0.8, 0.4, -5.0), abs=1e-6)
    assert cal.rmse_calibrated < cal.rmse_spec


def test_ebitda_bridge_arithmetic():
    qs = ["2025Q3", "2026Q3"]
    panel = pd.DataFrame({
        "block_hours": [16_000, 17_000], "core_rev": [200.0, 214.0], "adj_ebitda": [80.0, np.nan],
        "direct_expenses": [180.0, np.nan], "fuel_costs": [40.0, np.nan],
        "depreciation_in_direct": [30.0, np.nan], "jet_intra_q_move": [0.0, 0.02],
    }, index=pd.Index(qs, name="quarter"))
    steps = pd.DataFrame([{"quarter_from": "2026Q3", "quarter_to": "", "amount_mcad_per_quarter": 4.5}])
    b = model.ebitda_bridge(panel, "2026Q3", hours_q=17_000, core_rev_q=214.0, steps=steps)
    rev_h, cost_h = 200e6 / 16_000, 110e6 / 16_000          # kontantkostnad = 180 − 40 − 30
    assert b["volume"] == pytest.approx(1_000 * (rev_h - cost_h) / 1e6)
    assert b["price_mix"] == pytest.approx(14.0 - 1_000 * rev_h / 1e6)
    assert b["cost_steps"] == -4.5
    assert b["fuel_lag"] == 0.0                               # rörelse under 10 %-tröskeln
    assert b["adj_ebitda"] == pytest.approx(80 + b["volume"] + b["price_mix"] - 4.5)
