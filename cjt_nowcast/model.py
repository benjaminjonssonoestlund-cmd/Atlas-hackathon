"""Relativ modell: YoY-förändringar, inte absoluta nivåer.

OMSÄTTNING
    y  = YoY kärnomsättning (domestic + ACMI + charter, exkl. bränsletillägg)
    x1 = YoY blocktimmar  (träning: rapporterade totala; prognos: ADS-B,
         segmentviktat med förra årets segmentintäkter när segmenttimmar finns)
    x2 = YoY jet fuel (EIA USGC, kvartalsmedel)
    x3 = YoY USD/CAD (kvartalsmedel)
    OLS och Ridge (standardiserat X, alfa via tidsseriekorsvalidering). Ridge
    väljs om X är illa konditionerat eller om den korsvaliderar bättre.

    Blocktimmar per segment finns INTE i MD&A — bara totalen. Historisk träning
    (2017–) sker därför på total-YoY; segmentvikten används bara i prognosen
    där ADS-B ger segmenttimmar. Det är en medveten asymmetri, redovisad i README.

BRÄNSLETILLÄGG
    fs = k · jet_CAD · timmar, k = median av de fyra senast rapporterade kvartalen.

JUSTERAD EBITDA (specens brygga)
    ΔEBITDA ≈ Δtimmar · (intäkt/h − direkt kontantkostnad/h)   [volym]
            + (Δkärnomsättning − Δtimmar · intäkt/h)            [pris/mix]
            − Δkända kostnadssteg (manual/cost_steps.csv)
            + bränslelag                                         [se nedan]
            + drift (medelresidual senaste 8 kvartalen)
    Direkt kontantkostnad = direkta kostnader − bränsle − avskrivningar i
    direkta kostnader (bränsle går vidare till kund, avskrivningar är inte
    EBITDA). Intäkt/h och kostnad/h tas från samma kvartal året innan.

BRÄNSLELAG
    När jet fuel rört sig >10 % inom kvartalet släpar tillägget efter.
    β skattas som residual-EBITDA/kärnomsättning mot rörelsen, bara på sådana
    kvartal (regression genom origo). N och t-värde redovisas alltid.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.model_selection import TimeSeriesSplit

from .common import MANUAL, PROCESSED, quarters_between, shift_quarter

MDA_CSV = PROCESSED / "mda_quarterly.csv"
MARKET_CSV = PROCESSED / "market_quarterly.csv"
COST_STEPS_CSV = MANUAL / "cost_steps.csv"
FUEL_LAG_THRESHOLD = 0.10
DRIFT_WINDOW = 8
FS_K_WINDOW = 4
RIDGE_ALPHAS = np.logspace(-3, 2, 30)
CONDITION_LIMIT = 30.0
SEGMENTS = {"domestic": "domestic_rev", "acmi": "acmi_rev", "charter": "charter_rev"}


# ---------- Data ----------

def load_panel(first: str = "2016Q1", last: str | None = None) -> pd.DataFrame:
    """MD&A + marknad per kvartal, reindexerat så att shift(4) alltid är året innan."""
    mda = pd.read_csv(MDA_CSV).set_index("quarter")
    mkt = pd.read_csv(MARKET_CSV).set_index("quarter")
    last = last or max(mda.index.max(), mkt.index.max())
    idx = quarters_between(first, last)
    df = mda.reindex(idx).join(mkt.reindex(idx), how="left")
    df.index.name = "quarter"
    for col in ("domestic_rev", "acmi_rev", "charter_rev"):
        if col not in df:
            df[col] = np.nan
    if "core_rev" not in df or df["core_rev"].isna().all():
        df["core_rev"] = df[["domestic_rev", "acmi_rev", "charter_rev"]].sum(axis=1, min_count=3)
    return df


def yoy(s: pd.Series) -> pd.Series:
    return s / s.shift(4) - 1


def yoy_like_for_like(panel: pd.DataFrame, col: str) -> pd.Series:
    """YoY mot fjolårssiffran SOM DEN STÅR i kvartalets egen MD&A (py_*), annars shift(4).

    Cargojet har flyttat intäkter mellan segment (2021→2022) och omdefinierat
    justerad EBITDA (2020–2022). Original-q−4 mot q mäter då definitionsbyten,
    inte tillväxt. py_* finns bara EFTER q:s rapport, så detta används för
    TRÄNING och historiska residualer — aldrig som prognosbas."""
    base = panel[col].shift(4)
    py = f"py_{col}"
    if py in panel:
        base = panel[py].where(panel[py].notna(), base)
    return panel[col] / base - 1


def like_for_like_base(panel: pd.DataFrame, q: str) -> pd.Series:
    """Fjolårsraden för q med py_*-värden från q:s MD&A där de finns (för historik)."""
    base = panel.loc[shift_quarter(q, -4)].copy()
    for col in panel.columns:
        if col.startswith("py_") and pd.notna(panel.at[q, col]) and col[3:] in base.index:
            base[col[3:]] = panel.at[q, col]
    return base


def cost_steps() -> pd.DataFrame:
    if not COST_STEPS_CSV.exists():
        return pd.DataFrame(columns=["quarter_from", "quarter_to", "amount_mcad_per_quarter"])
    return pd.read_csv(COST_STEPS_CSV, dtype={"quarter_to": str}, keep_default_na=False)


def cost_step_level(q: str, steps: pd.DataFrame) -> float:
    total = 0.0
    for s in steps.itertuples():
        if s.quarter_from <= q and (not s.quarter_to or q <= s.quarter_to):
            total += float(s.amount_mcad_per_quarter)
    return total


def cost_step_yoy(q: str, steps: pd.DataFrame | None = None) -> float:
    steps = cost_steps() if steps is None else steps
    return cost_step_level(q, steps) - cost_step_level(shift_quarter(q, -4), steps)


# ---------- Omsättningsmodell ----------

@dataclass
class RevenueFit:
    kind: str                       # "ols" | "ridge"
    intercept: float
    coef: dict[str, float]          # originalskala
    alpha: float | None
    n: int
    r2_in_sample: float
    cv_rmse_ols: float
    cv_rmse_ridge: float
    condition_number: float
    train_quarters: list[str] = field(default_factory=list)

    FEATURES = ("hours_yoy", "fuel_yoy", "fx_yoy")

    def predict(self, hours_yoy: float, fuel_yoy: float, fx_yoy: float) -> float:
        x = {"hours_yoy": hours_yoy, "fuel_yoy": fuel_yoy, "fx_yoy": fx_yoy}
        return self.intercept + sum(self.coef[k] * x[k] for k in self.FEATURES)

    def implied_hours_yoy(self, core_rev_yoy: float, fuel_yoy: float, fx_yoy: float) -> float:
        """Vilken blocktimmes-YoY en given kärnomsättnings-YoY motsvarar (baklänges)."""
        b = self.coef["hours_yoy"]
        if abs(b) < 1e-9:
            return float("nan")
        return (core_rev_yoy - self.intercept - self.coef["fuel_yoy"] * fuel_yoy
                - self.coef["fx_yoy"] * fx_yoy) / b


def revenue_design(panel: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "y": yoy_like_for_like(panel, "core_rev"),
        "hours_yoy": yoy_like_for_like(panel, "block_hours"),
        "fuel_yoy": yoy(panel["jet_usd_gal"]),
        "fx_yoy": yoy(panel["usdcad"]),
    }, index=panel.index)


def _cv_rmse(model_factory, X: np.ndarray, y: np.ndarray) -> float:
    splits = min(5, max(2, len(y) // 6))
    errs = []
    for tr, te in TimeSeriesSplit(n_splits=splits).split(X):
        if len(tr) < 6:
            continue
        m = model_factory().fit(X[tr], y[tr])
        errs.append((m.predict(X[te]) - y[te]) ** 2)
    return float(np.sqrt(np.concatenate(errs).mean())) if errs else float("nan")


# COVID-chocken (volym- och charterboom, sedan normalisering) — bara för diagnos
COVID_QUARTERS = tuple(quarters_between("2020Q2", "2021Q4"))


def fit_revenue(panel: pd.DataFrame, before: str | None = None, min_obs: int = 12,
                exclude: tuple[str, ...] = ()) -> RevenueFit:
    d = revenue_design(panel).dropna()
    if before:
        d = d[d.index < before]
    if exclude:
        d = d[~d.index.isin(exclude)]
    if len(d) < min_obs:
        raise ValueError(f"för få kvartal att skatta omsättningsmodellen ({len(d)} < {min_obs})")
    X = d[list(RevenueFit.FEATURES)].to_numpy(float)
    y = d["y"].to_numpy(float)
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd
    cond = float(np.linalg.cond(np.c_[np.ones(len(Z)), Z]))

    cv_ols = _cv_rmse(LinearRegression, Z, y)
    # Ridge-alfa väljs på tidsserie-CV, inte slumpmässig KFold (kvartal är autokorrelerade)
    best_alpha, cv_ridge = None, float("inf")
    for a in RIDGE_ALPHAS:
        e = _cv_rmse(lambda a=a: Ridge(alpha=a), Z, y)
        if e < cv_ridge:
            best_alpha, cv_ridge = float(a), e
    use_ridge = cond > CONDITION_LIMIT or cv_ridge < cv_ols * 0.97
    m = Ridge(alpha=best_alpha).fit(Z, y) if use_ridge else LinearRegression().fit(Z, y)
    beta = m.coef_ / sd
    intercept = float(m.intercept_ - (beta * mu).sum())
    return RevenueFit(
        kind="ridge" if use_ridge else "ols", intercept=intercept,
        coef=dict(zip(RevenueFit.FEATURES, map(float, beta))),
        alpha=best_alpha if use_ridge else None, n=len(y),
        r2_in_sample=float(m.score(Z, y)), cv_rmse_ols=cv_ols, cv_rmse_ridge=cv_ridge,
        condition_number=cond, train_quarters=list(d.index))


# ---------- Bränsletillägg ----------

def fuel_surcharge_k(panel: pd.DataFrame, before: str) -> float:
    h = panel[panel.index < before].dropna(subset=["fuel_surcharge_other_rev", "jet_cad_gal", "block_hours"])
    k = h["fuel_surcharge_other_rev"] / (h["jet_cad_gal"] * h["block_hours"])
    return float(k.tail(FS_K_WINDOW).median())


# ---------- EBITDA ----------

def cash_direct_cost_ex_fuel(row: pd.Series) -> float:
    dep = row.get("depreciation_in_direct")
    fuel = row.get("fuel_costs")
    if pd.isna(row.get("direct_expenses")) or pd.isna(fuel) or pd.isna(dep):
        return float("nan")
    return float(row["direct_expenses"] - fuel - dep)


def ebitda_bridge(panel: pd.DataFrame, q: str, hours_q: float, core_rev_q: float,
                  drift: float = 0.0, fuel_lag_beta: float = 0.0,
                  steps: pd.DataFrame | None = None, base: pd.Series | None = None) -> dict:
    """Specens brygga för kvartal q givet timmar och kärnomsättning (prognos eller utfall).

    base: fjolårsraden. Standard = som ursprungligen rapporterad (prognosläge);
    historiken skickar in like_for_like_base."""
    base = panel.loc[shift_quarter(q, -4)] if base is None else base
    h0, r0 = base["block_hours"], base["core_rev"]
    rev_per_h = r0 * 1e6 / h0
    cost = cash_direct_cost_ex_fuel(base)
    cost_per_h = cost * 1e6 / h0 if pd.notna(cost) else float("nan")
    dh = hours_q - h0
    volume = dh * (rev_per_h - cost_per_h) / 1e6
    price_mix = (core_rev_q - r0) - dh * rev_per_h / 1e6
    step = cost_step_yoy(q, steps)
    move = panel.at[q, "jet_intra_q_move"] if q in panel.index else float("nan")
    lag = (fuel_lag_beta * move * core_rev_q
           if pd.notna(move) and abs(move) > FUEL_LAG_THRESHOLD else 0.0)
    ebitda = base["adj_ebitda"] + volume + price_mix - step + lag + drift
    return {"quarter": q, "base_adj_ebitda": base["adj_ebitda"], "rev_per_h": rev_per_h,
            "cash_cost_per_h": cost_per_h, "d_hours": dh, "volume": volume,
            "price_mix": price_mix, "cost_steps": -step, "fuel_lag": lag, "drift": drift,
            "adj_ebitda": ebitda}


def ebitda_history(panel: pd.DataFrame, before: str) -> pd.DataFrame:
    """Bryggan på UTFALL (rapporterade timmar/omsättning) → residualer för drift och bränslelag."""
    rows = []
    for q in panel.index:
        if q >= before or shift_quarter(q, -4) not in panel.index:
            continue
        r, b = panel.loc[q], like_for_like_base(panel, q)
        needed = [r["block_hours"], r["core_rev"], r["adj_ebitda"], b["block_hours"], b["core_rev"],
                  b["adj_ebitda"], cash_direct_cost_ex_fuel(b)]
        if any(pd.isna(v) for v in needed):
            continue
        br = ebitda_bridge(panel, q, r["block_hours"], r["core_rev"], base=b)
        rows.append({**br, "actual": r["adj_ebitda"], "residual": r["adj_ebitda"] - br["adj_ebitda"],
                     "core_rev": r["core_rev"], "jet_intra_q_move": r.get("jet_intra_q_move")})
    return pd.DataFrame(rows)


@dataclass
class BridgeCalibration:
    """ΔEBITDA + kostnadssteg = a·volym + b·pris/mix + c, skattat på historiken före q.

    Varför: specens brygga antar att pris/mix går 1:1 till EBITDA och att
    kostnad/h står still. Uppmätt 2023–2026 steg kontantkostnaden/h från ~5,1k
    till ~6,5k CAD och bryggan överskattade EBITDA med 10–30 mkr/kvartal.
    Kalibreringen låter historiken säga hur mycket av volym och pris/mix som
    faktiskt når EBITDA. Specens version rapporteras alltid bredvid."""
    a_volume: float
    b_price_mix: float
    c: float
    n: int
    rmse_spec: float       # specens brygga + drift, på samma kvartal
    rmse_calibrated: float  # in-sample


BRIDGE_WINDOW = 12


def fit_bridge(hist: pd.DataFrame, window: int = BRIDGE_WINDOW) -> BridgeCalibration | None:
    h = hist.dropna(subset=["volume", "price_mix", "actual", "base_adj_ebitda"]).tail(window)
    if len(h) < 8:
        return None
    y = (h["actual"] - h["base_adj_ebitda"] - h["cost_steps"]).to_numpy(float)
    X = np.c_[h["volume"], h["price_mix"], np.ones(len(h))]
    (a, b, c), *_ = np.linalg.lstsq(X, y, rcond=None)
    spec_err = h["residual"] - h["residual"].mean()          # spec + drift (medelresidual)
    cal_err = y - X @ np.array([a, b, c])
    return BridgeCalibration(float(a), float(b), float(c), len(h),
                             float(np.sqrt((spec_err ** 2).mean())), float(np.sqrt((cal_err ** 2).mean())))


@dataclass
class FuelLag:
    beta: float
    n: int
    t_stat: float
    quarters: list[str]


def fit_fuel_lag(hist: pd.DataFrame) -> FuelLag:
    """Marginaleffekt av stor bränslerörelse inom kvartalet, på residual-EBITDA/kärnomsättning."""
    h = hist.dropna(subset=["jet_intra_q_move"])
    h = h[h["jet_intra_q_move"].abs() > FUEL_LAG_THRESHOLD]
    if len(h) < 3:
        return FuelLag(0.0, len(h), float("nan"), list(h["quarter"]))
    x = h["jet_intra_q_move"].to_numpy(float)
    y = (h["residual"] / h["core_rev"]).to_numpy(float)
    beta = float((x @ y) / (x @ x))
    resid = y - beta * x
    se = float(np.sqrt((resid @ resid) / (len(x) - 1) / (x @ x)))
    return FuelLag(beta, len(h), beta / se if se else float("nan"), list(h["quarter"]))


# ---------- Marginalkontroll ----------

# Användarens låg/hög-kontroll för Q3 2026-rapporten (justerad EBITDA / omsättning).
# Motsvarar uppmätt spann 2024Q1–2026Q2 (31,3–34,3 %) — alltså FÖRE Q3 2026. Används
# därför bara framåt; backtestet använder margin_band_before (inget läckage).
MARGIN_LOW, MARGIN_HIGH = 0.31, 0.34
MARGIN_BAND_WINDOW = 8


def margin_band_before(panel: pd.DataFrame, q: str, n: int = MARGIN_BAND_WINDOW) -> dict:
    """Min/max justerad EBITDA-marginal över de n senast RAPPORTERADE kvartalen strikt före q.

    Kvartal q själv och allt efter ingår aldrig — annars testar backtestet mot
    ett band som delvis är byggt av facit."""
    hist = panel[(panel.index < q)].dropna(subset=["adj_ebitda", "total_rev"]).tail(n)
    m = hist["adj_ebitda"] / hist["total_rev"]
    return {"band_low": float(m.min()) if len(m) else np.nan,
            "band_high": float(m.max()) if len(m) else np.nan,
            "band_quarters": list(hist.index)}


def margin_check(total_rev: float, ebitda: float, low: float, high: float) -> dict:
    margin = ebitda / total_rev if total_rev else np.nan
    return {"model_margin": margin,
            "ebitda_at_low": total_rev * low, "ebitda_at_high": total_rev * high,
            "margin_low": low, "margin_high": high,
            "margin_inside": bool(low <= margin <= high) if pd.notna(margin) else None}


# ---------- Hela prognosen för ett kvartal ----------

def forecast_quarter(panel: pd.DataFrame, q: str, hours_yoy: float,
                     hours_yoy_for_fs: float | None = None,
                     exclude: tuple[str, ...] = COVID_QUARTERS) -> dict:
    """Prognos för q med all information före q:s rapport. `hours_yoy` från ADS-B.

    exclude: COVID-kvartalen är standard ute ur träningen — strukturbrott som
    trefaldigade CV-felet (0,078 → 0,18–0,20). Skicka () för diagnos med allt."""
    fit = fit_revenue(panel, before=q, exclude=exclude)
    prev = panel.loc[shift_quarter(q, -4)]
    fuel_yoy = panel.at[q, "jet_usd_gal"] / prev["jet_usd_gal"] - 1
    fx_yoy = panel.at[q, "usdcad"] / prev["usdcad"] - 1
    core_yoy = fit.predict(hours_yoy, fuel_yoy, fx_yoy)
    core_rev = prev["core_rev"] * (1 + core_yoy)
    hours = prev["block_hours"] * (1 + (hours_yoy if hours_yoy_for_fs is None else hours_yoy_for_fs))

    k = fuel_surcharge_k(panel, before=q)
    fs = k * panel.at[q, "jet_cad_gal"] * hours
    # Median av fyra senaste kvartal; saknat = 0 (före 2023Q4 låg posten inbakad i
    # bränsletillägget). Senaste värdet ensamt är fel: 2023Q4 hade −32,8 i engångsuppkomst.
    amort_hist = panel.loc[panel.index < q, "amortization_contract_assets"].tail(4).fillna(0.0)
    amort = float(amort_hist.median()) if len(amort_hist) else 0.0

    hist = ebitda_history(panel, before=q)
    lag = fit_fuel_lag(hist) if len(hist) else FuelLag(0.0, 0, float("nan"), [])
    drift = float(hist["residual"].tail(DRIFT_WINDOW).mean()) if len(hist) else 0.0
    bridge = ebitda_bridge(panel, q, hours, core_rev, drift=drift, fuel_lag_beta=lag.beta)
    cal = fit_bridge(hist) if len(hist) else None
    spec_ebitda = bridge["adj_ebitda"]
    if cal is not None:
        bridge["adj_ebitda"] = (bridge["base_adj_ebitda"] + cal.a_volume * bridge["volume"]
                                + cal.b_price_mix * bridge["price_mix"] + cal.c
                                + bridge["cost_steps"] + bridge["fuel_lag"])
    return {
        "ebitda_method": "calibrated" if cal else "spec+drift",
        "ebitda_spec_adj_ebitda": spec_ebitda,
        "bridge_a_volume": cal.a_volume if cal else np.nan,
        "bridge_b_price_mix": cal.b_price_mix if cal else np.nan,
        "bridge_c": cal.c if cal else np.nan,
        "bridge_n": cal.n if cal else 0,
        "bridge_rmse_spec": cal.rmse_spec if cal else np.nan,
        "bridge_rmse_calibrated": cal.rmse_calibrated if cal else np.nan,
        "quarter": q, "model": fit.kind, "n_train": fit.n, "cv_rmse": min(fit.cv_rmse_ols, fit.cv_rmse_ridge),
        "coef_hours": fit.coef["hours_yoy"], "coef_fuel": fit.coef["fuel_yoy"], "coef_fx": fit.coef["fx_yoy"],
        "hours_yoy": hours_yoy, "fuel_yoy": fuel_yoy, "fx_yoy": fx_yoy,
        "core_rev_yoy": core_yoy, "core_rev": core_rev, "block_hours": hours,
        "fuel_surcharge_other_rev": fs, "amortization_contract_assets": amort,
        "total_rev": core_rev + fs + amort,
        "fuel_lag_beta": lag.beta, "fuel_lag_n": lag.n, "fuel_lag_t": lag.t_stat,
        **{f"ebitda_{k}": v for k, v in bridge.items() if k != "quarter"},
        "_fit": fit,
    }


def implied_consensus_hours(panel: pd.DataFrame, q: str, fc: dict, consensus_total_rev: float) -> dict:
    """Baklängesräkning: vilka blocktimmar konsensusomsättningen implicerar i vår modell."""
    fit: RevenueFit = fc["_fit"]
    prev = panel.loc[shift_quarter(q, -4)]
    # konsensus är TOTAL omsättning — dra av vår bränsletilläggs- och amorteringsprognos
    core = consensus_total_rev - fc["fuel_surcharge_other_rev"] - fc["amortization_contract_assets"]
    core_yoy = core / prev["core_rev"] - 1
    h_yoy = fit.implied_hours_yoy(core_yoy, fc["fuel_yoy"], fc["fx_yoy"])
    return {"implied_core_rev": core, "implied_core_rev_yoy": core_yoy,
            "implied_hours_yoy": h_yoy, "implied_block_hours": prev["block_hours"] * (1 + h_yoy)}
