"""Backtest 2024Q1–2026Q2 och nowcast för 2026Q3.

INFORMATIONSDISCIPLIN: prognosen för kvartal q använder bara det som fanns före
q:s rapport —
  - modellen skattas på kvartal < q (MD&A för q−1 är publicerad),
  - blocktimmar kommer från ADS-B för q (inte från q:s MD&A),
  - kalibreringen ADS-B→rapporterat tas bara från kvartal < q,
  - marknadsdata för hela q (kvartalet är slut långt före rapporten).

BLOCKTIMMES-YoY ur ADS-B, i prioritetsordning:
  1. ADS-B mot ADS-B samma kalenderdagar året innan — täckningsbias i
     adsb.lol tar i stort sett ut sig. Segmentviktat med förra årets
     segmentintäkter om ≥80 % av timmarna är klassade.
  2. (2024, inget ADS-B året innan) kalibrerad ADS-B-nivå mot rapporterade
     timmar året innan. För 2024Q1 finns ingen tidigare kalibrering → faktor 1,
     flaggas "uncalibrated".
Som diagnos körs samma modell även med RAPPORTERADE timmar ("perfect hours"),
så att fel från ADS-B-mätningen skiljs från fel i själva modellen.

EPS: justerad EPS ≈ fjolårets + ΔEBITDA·(1−skatt)/aktier. Grovt (ignorerar
förändrade avskrivningar och räntor) — används bara för RIKTNINGEN mot konsensus.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import adsb, consensus, flights, market, model, routes, validate
from .common import PROCESSED, quarter_bounds, quarters_between, shift_quarter

LOG = logging.getLogger("cjt.backtest")

BACKTEST_CSV = PROCESSED / "backtest.csv"
NOWCAST_CSV = PROCESSED / "nowcast_{q}.csv"
NOWCAST_MD = PROCESSED / "nowcast_{q}.md"
TAX_RATE = 0.265          # kanadensisk kombinerad bolagsskatt, antagande
ET = ZoneInfo("America/Toronto")
MIN_COVERAGE = 0.5
MIN_CLASSIFIED_SHARE = 0.8


# ---------- ADS-B-timmar ----------

def prepare_flights(f: pd.DataFrame) -> pd.DataFrame:
    seg = routes.with_segments(f)
    seg["off_date_et"] = pd.to_datetime(seg["off_block"], unit="s", utc=True).dt.tz_convert(ET).dt.date
    return seg


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(i) for i in range((end - start).days + 1)]


def window_hours(seg: pd.DataFrame, start: date, end: date) -> tuple[pd.Series, float]:
    """Blocktimmar per segment i [start, end], uppskalade för dagar utan dump."""
    days = _days(start, end)
    cov = sum(adsb.is_day_done(d) for d in days) / len(days)
    sel = seg[(seg["off_date_et"] >= start) & (seg["off_date_et"] <= end)]
    h = sel.groupby("segment")["block_h"].sum()
    return (h / cov if cov else h * np.nan), cov


def year_before(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:        # 29 feb
        return d - timedelta(days=365)


def calibration_before(val: pd.DataFrame, q: str, n: int = 4) -> float | None:
    v = val[(val["quarter"] < q) & val["calibration"].notna() & (val["day_coverage"] > MIN_COVERAGE)]
    return float(v["calibration"].tail(n).median()) if len(v) else None


def measured_hours_yoy(seg: pd.DataFrame, panel: pd.DataFrame, val: pd.DataFrame,
                       q: str, cutoff: date | None = None) -> dict:
    start, end = quarter_bounds(q)
    end = min(end, cutoff) if cutoff else end
    h_now, cov_now = window_hours(seg, start, end)
    h_prev, cov_prev = window_hours(seg, year_before(start), year_before(end))
    prev = panel.loc[shift_quarter(q, -4)]
    out = {"window_start": start, "window_end": end, "window_days": len(_days(start, end)),
           "coverage_now": cov_now, "coverage_prev": cov_prev,
           "adsb_hours_now": float(h_now.sum()), "adsb_hours_prev_same_days": float(h_prev.sum())}
    for s in ("domestic", "acmi", "charter", "ferry", "unclassified"):
        out[f"h_{s}"] = float(h_now.get(s, 0.0))
        out[f"h_prev_{s}"] = float(h_prev.get(s, 0.0))
    classified = sum(h_now.get(s, 0.0) for s in model.SEGMENTS)
    out["classified_share"] = classified / h_now.sum() if h_now.sum() else np.nan

    if cov_now >= MIN_COVERAGE and cov_prev >= MIN_COVERAGE and h_prev.sum() > 0:
        total = h_now.sum() / h_prev.sum() - 1
        out["hours_yoy_total"] = float(total)
        seg_yoy = {s: h_now.get(s, 0.0) / h_prev[s] - 1 for s in model.SEGMENTS
                   if h_prev.get(s, 0.0) > 0}
        for s, v in seg_yoy.items():
            out[f"yoy_{s}"] = float(v)
        weights = {s: prev[col] / prev["core_rev"] for s, col in model.SEGMENTS.items()}
        if len(seg_yoy) == 3 and out["classified_share"] >= MIN_CLASSIFIED_SHARE \
                and all(pd.notna(w) for w in weights.values()):
            out["hours_yoy"] = float(sum(weights[s] * seg_yoy[s] for s in seg_yoy) / sum(weights.values()))
            out["hours_method"] = "adsb_yoy_segment_weighted"
        else:
            out["hours_yoy"] = float(total)
            out["hours_method"] = "adsb_yoy_total"
    elif cov_now >= MIN_COVERAGE and pd.notna(prev["block_hours"]) and cutoff is None:
        calib = calibration_before(val, q)
        out["calibration"] = calib
        out["hours_yoy"] = float(h_now.sum() * (calib or 1.0) / prev["block_hours"] - 1)
        out["hours_method"] = "adsb_level_calibrated" if calib else "adsb_level_uncalibrated"
    else:
        out["hours_yoy"] = np.nan
        out["hours_method"] = "insufficient_coverage"
    return out


LAG_SAME_WEEKDAY = 364


def matched_day_yoy(seg: pd.DataFrame, days_now: list[date], panel: pd.DataFrame, q: str) -> dict:
    """Stickprovs-YoY: blocktimmar på dagar d mot samma veckodag d−364, bara par där båda är hämtade.

    Cargojets nätverk går i veckoschema — veckodagsmatchning tar bort helgeffekten
    (uppmätt: 12–19 plan i luften lör/sön mot 35+ vardagar). Dagarna räknas i UTC,
    samma som dumpfilerna."""
    pairs = [(d, d - timedelta(days=LAG_SAME_WEEKDAY)) for d in days_now
             if adsb.is_day_done(d) and adsb.is_day_done(d - timedelta(days=LAG_SAME_WEEKDAY))]
    now_days, prev_days = {p[0] for p in pairs}, {p[1] for p in pairs}
    h_now = seg[seg["off_date_utc"].isin(now_days)].groupby("segment")["block_h"].sum()
    h_prev = seg[seg["off_date_utc"].isin(prev_days)].groupby("segment")["block_h"].sum()
    c_now = seg[seg["off_date_utc"].isin(now_days)]["cycles"].sum()
    c_prev = seg[seg["off_date_utc"].isin(prev_days)]["cycles"].sum()
    out = {"pairs": [(str(a), str(b)) for a, b in pairs], "n_pairs": len(pairs),
           "adsb_hours_now": float(h_now.sum()), "adsb_hours_prev_same_days": float(h_prev.sum()),
           "cycles_now": int(c_now), "cycles_prev": int(c_prev),
           "cycles_yoy": c_now / c_prev - 1 if c_prev else np.nan,
           "window_days": len(pairs), "coverage_now": np.nan, "coverage_prev": np.nan}
    for s in ("domestic", "acmi", "charter", "ferry", "unclassified"):
        out[f"h_{s}"] = float(h_now.get(s, 0.0))
        out[f"h_prev_{s}"] = float(h_prev.get(s, 0.0))
    if not pairs or h_prev.sum() == 0:
        out.update(hours_yoy=np.nan, hours_method="no_matched_days")
        return out
    out["hours_yoy_total"] = float(h_now.sum() / h_prev.sum() - 1)
    seg_yoy = {s: h_now.get(s, 0.0) / h_prev[s] - 1 for s in model.SEGMENTS if h_prev.get(s, 0.0) > 0}
    for s, v in seg_yoy.items():
        out[f"yoy_{s}"] = float(v)
    classified = sum(h_now.get(s, 0.0) for s in model.SEGMENTS)
    out["classified_share"] = classified / h_now.sum() if h_now.sum() else np.nan
    prev = panel.loc[shift_quarter(q, -4)]
    weights = {s: prev[col] / prev["core_rev"] for s, col in model.SEGMENTS.items()}
    if len(seg_yoy) == 3 and out["classified_share"] >= MIN_CLASSIFIED_SHARE:
        out["hours_yoy"] = float(sum(weights[s] * seg_yoy[s] for s in seg_yoy) / sum(weights.values()))
        out["hours_method"] = f"sample_{len(pairs)}d_segment_weighted"
    else:
        out["hours_yoy"] = out["hours_yoy_total"]
        out["hours_method"] = f"sample_{len(pairs)}d_total"
    return out


SURPRISE_CSV = PROCESSED / "surprise_vs_consensus.csv"
SURPRISE_MD = PROCESSED / "surprise_vs_consensus.md"
# Bara omsättning mäts mot konsensus (användarbeslut 2026-09-13): blocktimmar driver
# omsättningen direkt; EBITDA/EPS beror på kostnads- och engångsposter som modellen inte ser.
METRICS = (("Omsättning", "total_rev", "consensus_revenue_mcad", "total_rev"),)


def _direction(x: float) -> str:
    if pd.isna(x):
        return "–"
    return "ÖVER" if x > 0 else "UNDER" if x < 0 else "LIKA"


def surprise_table(first: str = "2024Q2", last: str = "2026Q2", nowcast_q: str = "2026Q3") -> pd.DataFrame:
    """Kärnutdata: prognos ÖVER/UNDER konsensus → utfall ÖVER/UNDER → rätt riktning?

    Historiska kvartal: modellen skattad på kvartal < q, med q:s RAPPORTERADE
    blocktimmar (full ADS-B saknas för perioden) — dvs. modellens träffbild om
    timmarna mätts perfekt. Utfall ur MD&A. nowcast_q: ADS-B-stickprovet,
    utfall saknas tills rapporten."""
    panel = model.load_panel(last=nowcast_q)
    cons = consensus.load().set_index("quarter")
    prices = _load_prices()
    rows = []
    for q in quarters_between(first, last):
        a, p = panel.loc[q], panel.loc[shift_quarter(q, -4)]
        base_h = a["py_block_hours"] if pd.notna(a.get("py_block_hours")) else p["block_hours"]
        fc = model.forecast_quarter(panel, q, a["block_hours"] / base_h - 1)
        c = cons.loc[q] if q in cons.index else pd.Series(dtype=object)
        move = report_day_move(prices, a["mda_date"])["move_pct"]
        for name, fkey, ckey, akey in METRICS:
            rows.append(_surprise_row(q, a["mda_date"], "rapporterade (MD&A)", name,
                                      fc[fkey], c.get(ckey, np.nan), a[akey], move,
                                      hours=a["block_hours"], hours_yoy=a["block_hours"] / base_h - 1))
    nc = Path(str(NOWCAST_CSV).format(q=nowcast_q))
    if nc.exists():
        o = pd.read_csv(nc).iloc[0]
        c = cons.loc[nowcast_q] if nowcast_q in cons.index else pd.Series(dtype=object)
        src = f"ADS-B stickprov ({o.get('hours_method', '')})"
        for name, fkey, ckey, _ in METRICS:
            rows.append(_surprise_row(nowcast_q, c.get("report_date"), src, name,
                                      o.get(fkey, np.nan), c.get(ckey, np.nan), np.nan, np.nan,
                                      hours=o.get("block_hours", np.nan), hours_yoy=o.get("hours_yoy", np.nan)))
    df = pd.DataFrame(rows)
    SURPRISE_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(SURPRISE_CSV, index=False)
    SURPRISE_MD.write_text(surprise_markdown(df), encoding="utf-8")
    return df


def _surprise_row(q, report_date, hours_src, metric, fc, cons, actual, move,
                  hours=np.nan, hours_yoy=np.nan) -> dict:
    pred = _direction(fc - cons) if pd.notna(fc) and pd.notna(cons) else "–"
    act = _direction(actual - cons) if pd.notna(actual) and pd.notna(cons) else "–"
    return {"kvartal": q, "rapportdag": report_date, "timmar": hours_src,
            "blocktimmar_indata": hours, "timmar_yoy": hours_yoy, "mått": metric,
            "prognos": fc, "konsensus": cons, "prognos_vs_konsensus": pred,
            "utfall": actual, "utfall_vs_konsensus": act,
            "rätt": (pred == act) if "–" not in (pred, act) else None,
            "kurs_rapportdag_pct": move}


def surprise_markdown(df: pd.DataFrame) -> str:
    hist = df[df["timmar"] == "rapporterade (MD&A)"]
    scored = hist["rätt"].dropna()
    out = ["# Omsättning mot konsensus — Cargojet (CJT)", "",
           f"**{int(scored.sum())}/{len(scored)} rätt riktning** i {hist['kvartal'].nunique()} historiska tester "
           f"({hist['kvartal'].min()}–{hist['kvartal'].max()}). Indata: Cargojets **rapporterade blocktimmar** ur MD&A; "
           "modellen skattas bara på kvartal före det testade; utfall ur MD&A. Sista raden är nowcasten "
           "(ADS-B-stickprov, utfall saknas).", "",
           "| Kvartal | Blocktimmar (indata) | Timmar YoY | Prognos | Konsensus | Prognos vs kons. | Utfall | "
           "Utfall vs kons. | Rätt? | Kurs rapportdag |",
           "|---|---:|---:|---:|---:|:---:|---:|:---:|:---:|---:|"]
    for r in df.itertuples():
        hit = "–" if r.rätt is None or pd.isna(r.rätt) else ("✓" if r.rätt else "✗")
        label = r.kvartal if r.timmar == "rapporterade (MD&A)" else f"{r.kvartal} (ADS-B)"
        out.append(f"| {label} | {_fmt(r.blocktimmar_indata, nd=0)} | {_fmt(r.timmar_yoy, pct=True)} | "
                   f"{_fmt(r.prognos)} | {_fmt(r.konsensus)} | {r.prognos_vs_konsensus} | {_fmt(r.utfall)} | "
                   f"{r.utfall_vs_konsensus} | {hit} | "
                   f"{'–' if pd.isna(r.kurs_rapportdag_pct) else f'{r.kurs_rapportdag_pct:+.1f} %'} |")
    out += ["", "*Mkr CAD. 2024Q1 saknar konsensus. 2026Q3: forward-konsensus per 2026-09-11. "
            "Ej finansiell rådgivning.*"]
    return "\n".join(out)


def _margin_fields(panel: pd.DataFrame, q: str, fc: dict) -> dict:
    """Användarens 31/34 %-kontroll + band ur kvartal strikt före q (inget facit från q)."""
    fixed = model.margin_check(fc["total_rev"], fc["ebitda_adj_ebitda"], model.MARGIN_LOW, model.MARGIN_HIGH)
    band = model.margin_band_before(panel, q)
    assert all(bq < q for bq in band["band_quarters"]), "marginalbandet får inte innehålla testkvartalet"
    return {**fixed, "band_low_pre_q": band["band_low"], "band_high_pre_q": band["band_high"],
            "band_quarters": band["band_quarters"],
            "model_margin_inside_band_pre_q": bool(band["band_low"] <= fixed["model_margin"] <= band["band_high"])}


def weekday_scaled_hours(seg: pd.DataFrame, days: list[date], q: str) -> float:
    """Kvartalsnivå ur stickprov: medel per veckodag × antal sådana veckodagar i kvartalet."""
    days = [d for d in days if adsb.is_day_done(d)]
    per_day = (seg[seg["off_date_utc"].isin(days)].groupby("off_date_utc")["block_h"].sum()
               .reindex(days, fill_value=0.0))
    by_wd = per_day.groupby([d.weekday() for d in per_day.index]).mean()
    start, end = quarter_bounds(q)
    return float(sum(by_wd.get(d.weekday(), per_day.mean()) for d in _days(start, end)))


def run_nowcast_sampled(q: str, days_now: list[date], flights_csv=None) -> dict:
    """Nowcast på stickprovsdagar (se matched_day_yoy). Skriver samma rapport som run_nowcast."""
    panel = model.load_panel(last=q)
    f = flights.read_flights_csv(flights_csv or (PROCESSED / "flights_sampled.csv.gz"))
    f["off_date_utc"] = pd.to_datetime(f["off_date_utc"]).dt.date
    seg = routes.with_segments(f)
    hy = matched_day_yoy(seg, days_now, panel, q)
    if pd.isna(hy["hours_yoy"]):
        raise RuntimeError(f"inga matchade dagpar för {q}")
    fc = model.forecast_quarter(panel, q, hy["hours_yoy"])
    cons = consensus.load().set_index("quarter")
    c = cons.loc[q] if q in cons.index else pd.Series(dtype=object)
    out = {"quarter": q, "adsb_through": max(d for d, _ in hy["pairs"]), **hy,
           **{k: v for k, v in fc.items() if not k.startswith("_")}}
    cons_rev, cons_eps = c.get("consensus_revenue_mcad"), c.get("consensus_eps_adj_cad")
    out["consensus_revenue_mcad"], out["consensus_eps_adj_cad"] = cons_rev, cons_eps
    out["consensus_is_forward"] = bool(c.get("is_forward", False))
    out["consensus_note"] = c.get("notes", "")
    if pd.notna(cons_rev):
        out["diff_rev_vs_consensus_mcad"] = fc["total_rev"] - cons_rev
        out["diff_rev_vs_consensus_pct"] = 100 * (fc["total_rev"] / cons_rev - 1)
        out.update(model.implied_consensus_hours(panel, q, fc, cons_rev))
    p = panel.loc[shift_quarter(q, -4)]
    sh = shares_m(p)
    if pd.notna(cons_eps) and pd.notna(sh):
        eps_hat = p["adj_eps"] + (fc["ebitda_adj_ebitda"] - p["adj_ebitda"]) * (1 - TAX_RATE) / sh
        out["fc_adj_eps"], out["diff_eps_vs_consensus"] = eps_hat, eps_hat - cons_eps
    out.update(_margin_fields(panel, q, fc))
    pd.DataFrame([{k: v for k, v in out.items() if k not in ("pairs", "band_quarters")}]).to_csv(
        str(NOWCAST_CSV).format(q=q), index=False)
    md = _nowcast_markdown(out, fc["_fit"]).replace(
        "dagar av kvartalet", f"matchade veckodagspar (stickprov: {', '.join(a for a, _ in hy['pairs'])})")
    with open(str(NOWCAST_MD).format(q=q), "w", encoding="utf-8") as fh:
        fh.write(md)
    return out


# ---------- Hjälp ----------

def shares_m(row: pd.Series) -> float:
    if pd.notna(row.get("diluted_shares_m")):
        return float(row["diluted_shares_m"])
    if pd.notna(row.get("adj_earnings")) and pd.notna(row.get("adj_eps")) and row["adj_eps"]:
        return float(row["adj_earnings"] / row["adj_eps"])
    return float("nan")


def report_day_move(prices: pd.DataFrame | None, report_date: str | None) -> dict:
    """Cargojet rapporterar efter stängning → reaktion = nästa handelsdags stängning / rapportdagens."""
    if prices is None or not report_date or pd.isna(report_date):
        return {"px_before": np.nan, "px_after": np.nan, "move_pct": np.nan}
    d = date.fromisoformat(str(report_date)[:10])
    before = prices[prices["date"] <= d].tail(1)
    after = prices[prices["date"] > d].head(1)
    if before.empty or after.empty:
        return {"px_before": np.nan, "px_after": np.nan, "move_pct": np.nan}
    b, a = float(before["close"].iloc[0]), float(after["close"].iloc[0])
    return {"px_before": b, "px_after": a, "move_pct": 100 * (a / b - 1)}


def _sign(x: float) -> float:
    return float(np.sign(x)) if pd.notna(x) else np.nan


def _load_prices() -> pd.DataFrame | None:
    try:
        return market.prices_daily()
    except Exception as exc:  # noqa: BLE001 — kursreaktion är bonus, inte en förutsättning
        LOG.warning("CJT-kurser från stooq saknas (%s) — rörelse rapportdagen lämnas tom", exc)
        return None


# ---------- Backtest ----------

def run_backtest(first: str = "2024Q1", last: str = "2026Q2") -> pd.DataFrame:
    panel = model.load_panel()
    fl = flights.load()
    seg = prepare_flights(fl)
    val = validate.block_hours_table(fl, panel.reset_index(), first, last)
    cons = consensus.load().set_index("quarter")
    prices = _load_prices()
    rows = []
    for q in quarters_between(first, last):
        a, p = panel.loc[q], panel.loc[shift_quarter(q, -4)]
        hy = measured_hours_yoy(seg, panel, val, q)
        row = {"quarter": q, **{k: v for k, v in hy.items() if not k.startswith("h_prev")}}
        if pd.isna(hy["hours_yoy"]):
            rows.append({**row, "error": "för lite ADS-B-täckning"})
            continue
        try:
            fc = model.forecast_quarter(panel, q, hy["hours_yoy"])
            perfect = model.forecast_quarter(panel, q, a["block_hours"] / p["block_hours"] - 1)
        except (ValueError, KeyError) as exc:
            rows.append({**row, "error": str(exc)})
            continue
        c = cons.loc[q] if q in cons.index else pd.Series(dtype=object)
        sh = shares_m(p)
        eps_hat = p["adj_eps"] + (fc["ebitda_adj_ebitda"] - p["adj_ebitda"]) * (1 - TAX_RATE) / sh \
            if pd.notna(sh) else np.nan
        cons_rev, cons_eps = c.get("consensus_revenue_mcad"), c.get("consensus_eps_adj_cad")
        # Förankra i MD&A-datumet: Investing.com ligger +1 dag 2025Q1–Q3. Fönstret
        # stängning(MD&A-dag) → stängning(nästa handelsdag) fångar både släpp efter
        # stängning samma dag och före öppning nästa dag.
        report_date = a.get("mda_date") if pd.notna(a.get("mda_date")) else c.get("report_date")
        row.update({
            "model": fc["model"], "n_train": fc["n_train"],
            "reported_hours_yoy": a["block_hours"] / p["block_hours"] - 1,
            "fc_total_rev": fc["total_rev"], "act_total_rev": a["total_rev"],
            "fc_core_rev": fc["core_rev"], "act_core_rev": a["core_rev"],
            "fc_adj_ebitda": fc["ebitda_adj_ebitda"], "act_adj_ebitda": a["adj_ebitda"],
            "fc_adj_eps": eps_hat, "act_adj_eps": a["adj_eps"],
            "err_total_rev_pct": 100 * (fc["total_rev"] / a["total_rev"] - 1),
            "err_adj_ebitda_pct": 100 * (fc["ebitda_adj_ebitda"] / a["adj_ebitda"] - 1),
            "perfect_hours_err_total_rev_pct": 100 * (perfect["total_rev"] / a["total_rev"] - 1),
            "perfect_hours_err_adj_ebitda_pct": 100 * (perfect["ebitda_adj_ebitda"] / a["adj_ebitda"] - 1),
            "consensus_revenue_mcad": cons_rev, "consensus_eps_adj_cad": cons_eps,
            "report_date": report_date, "report_date_consensus_file": c.get("report_date"),
            "pred_rev_vs_cons": _sign(fc["total_rev"] - cons_rev) if pd.notna(cons_rev) else np.nan,
            "act_rev_vs_cons": _sign(a["total_rev"] - cons_rev) if pd.notna(cons_rev) else np.nan,
            "pred_eps_vs_cons": _sign(eps_hat - cons_eps) if pd.notna(cons_eps) else np.nan,
            "act_eps_vs_cons": _sign(a["adj_eps"] - cons_eps) if pd.notna(cons_eps) else np.nan,
            **report_day_move(prices, report_date),
        })
        band = model.margin_band_before(panel, q)          # bara kvartal < q
        mc = model.margin_check(fc["total_rev"], fc["ebitda_adj_ebitda"], band["band_low"], band["band_high"])
        row.update({"model_margin": mc["model_margin"], "band_low_pre_q": band["band_low"],
                    "band_high_pre_q": band["band_high"], "model_margin_inside_band": mc["margin_inside"],
                    "actual_margin": a["adj_ebitda"] / a["total_rev"],
                    "actual_margin_inside_band": bool(band["band_low"] <= a["adj_ebitda"] / a["total_rev"] <= band["band_high"])})
        row["hit_rev"] = (row["pred_rev_vs_cons"] == row["act_rev_vs_cons"]) if pd.notna(cons_rev) else None
        row["hit_eps"] = (row["pred_eps_vs_cons"] == row["act_eps_vs_cons"]) if pd.notna(cons_eps) else None
        rows.append(row)
    df = pd.DataFrame(rows)
    BACKTEST_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(BACKTEST_CSV, index=False)
    return df


def backtest_summary(df: pd.DataFrame) -> str:
    lines = []
    for m in ("rev", "eps"):
        col = f"hit_{m}"
        s = df[col].dropna() if col in df else pd.Series(dtype=object)
        lines.append(f"Riktning mot konsensus ({m}): {int(s.sum())}/{len(s)} träffar"
                     if len(s) else f"Riktning mot konsensus ({m}): ingen konsensus ifylld")
    for col, label in (("err_total_rev_pct", "omsättning"), ("err_adj_ebitda_pct", "just. EBITDA"),
                       ("perfect_hours_err_total_rev_pct", "omsättning m. rapporterade timmar")):
        if col in df and df[col].notna().any():
            lines.append(f"MAPE {label}: {df[col].abs().mean():.1f} %")
    return "\n".join(lines)


# ---------- Nowcast ----------

def last_done_day(q: str) -> date | None:
    start, end = quarter_bounds(q)
    done = [d for d in _days(start, min(end, date.today())) if adsb.is_day_done(d)]
    return max(done) if done else None


def run_nowcast(q: str = "2026Q3") -> dict:
    panel = model.load_panel(last=q)
    fl = flights.load()
    seg = prepare_flights(fl)
    val = validate.block_hours_table(fl, panel.reset_index(), "2024Q1", shift_quarter(q, -1))
    cutoff = last_done_day(q)
    if cutoff is None:
        raise RuntimeError(f"inga ADS-B-dagar hämtade för {q}")
    hy = measured_hours_yoy(seg, panel, val, q, cutoff=cutoff)
    fc = model.forecast_quarter(panel, q, hy["hours_yoy"])
    cons = consensus.load().set_index("quarter")
    c = cons.loc[q] if q in cons.index else pd.Series(dtype=object)
    out = {"quarter": q, "adsb_through": cutoff.isoformat(), **hy,
           **{k: v for k, v in fc.items() if not k.startswith("_")}}
    cons_rev, cons_eps = c.get("consensus_revenue_mcad"), c.get("consensus_eps_adj_cad")
    out["consensus_revenue_mcad"], out["consensus_eps_adj_cad"] = cons_rev, cons_eps
    out["consensus_is_forward"] = bool(c.get("is_forward", False))
    out["consensus_note"] = c.get("notes", "")
    if pd.notna(cons_rev):
        out["diff_rev_vs_consensus_mcad"] = fc["total_rev"] - cons_rev
        out["diff_rev_vs_consensus_pct"] = 100 * (fc["total_rev"] / cons_rev - 1)
        out.update(model.implied_consensus_hours(panel, q, fc, cons_rev))
    p = panel.loc[shift_quarter(q, -4)]
    sh = shares_m(p)
    if pd.notna(cons_eps) and pd.notna(sh):
        eps_hat = p["adj_eps"] + (fc["ebitda_adj_ebitda"] - p["adj_ebitda"]) * (1 - TAX_RATE) / sh
        out["fc_adj_eps"], out["diff_eps_vs_consensus"] = eps_hat, eps_hat - cons_eps
    out.update(_margin_fields(panel, q, fc))
    pd.DataFrame([{k: v for k, v in out.items() if k != "band_quarters"}]).to_csv(
        str(NOWCAST_CSV).format(q=q), index=False)
    md = _nowcast_markdown(out, fc["_fit"])
    path = str(NOWCAST_MD).format(q=q)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)
    return out


def _fmt(x, pct=False, nd=1) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "–"
    return f"{100 * x:+.{nd}f} %" if pct else f"{x:,.{nd}f}"


def _nowcast_markdown(o: dict, fit: model.RevenueFit) -> str:
    q = o["quarter"]
    seg_rows = "\n".join(
        f"| {s} | {_fmt(o.get(f'h_{s}'), nd=0)} | {_fmt(o.get(f'h_prev_{s}'), nd=0)} | {_fmt(o.get(f'yoy_{s}'), pct=True)} |"
        for s in ("domestic", "acmi", "charter", "ferry", "unclassified"))
    cons_block = ("Konsensus saknas i manual/consensus.csv — fyll i för skillnad och implicita timmar."
                  if pd.isna(o.get("consensus_revenue_mcad")) else
                  f"| | Modell | Konsensus | Skillnad |\n|---|---:|---:|---:|\n"
                  f"| Omsättning (mkr CAD) | {_fmt(o['total_rev'])} | {_fmt(o['consensus_revenue_mcad'])} | "
                  f"{_fmt(o.get('diff_rev_vs_consensus_mcad'))} ({_fmt(o.get('diff_rev_vs_consensus_pct'), nd=1)} %) |\n"
                  f"| Just. EPS (CAD) | {_fmt(o.get('fc_adj_eps'), nd=2)} | {_fmt(o.get('consensus_eps_adj_cad'), nd=2)} | "
                  f"{_fmt(o.get('diff_eps_vs_consensus'), nd=2)} |\n\n"
                  f"Implicit i konsensus: blocktimmar {_fmt(o.get('implied_block_hours'), nd=0)} "
                  f"({_fmt(o.get('implied_hours_yoy'), pct=True)} YoY) mot uppmätt {_fmt(o['hours_yoy'], pct=True)}.")
    return f"""# Nowcast {q} — Cargojet (CJT)

ADS-B t.o.m. **{o['adsb_through']}** ({o['window_days']} dagar av kvartalet{"" if pd.isna(o['coverage_now']) else f", dagtäckning {o['coverage_now']:.0%}; jämförelsefönster samma kalenderdagar året innan, täckning {o['coverage_prev']:.0%}"}).
Metod för timmes-YoY: `{o['hours_method']}`. Andel klassade timmar: {_fmt(o.get('classified_share'), pct=False, nd=2)}.

## Uppmätta blocktimmar per segment (samma dagar, YoY)

| Segment | {q} | Året innan | YoY |
|---|---:|---:|---:|
{seg_rows}
| **Totalt** | {_fmt(o['adsb_hours_now'], nd=0)} | {_fmt(o['adsb_hours_prev_same_days'], nd=0)} | {_fmt(o.get('hours_yoy_total'), pct=True)} |

Viktad timmes-YoY i modellen: **{_fmt(o['hours_yoy'], pct=True)}** → implicerade kvartalstimmar {_fmt(o['block_hours'], nd=0)}.

## Prognos (mkr CAD)

| Post | Prognos |
|---|---:|
| Kärnomsättning (domestic+ACMI+charter) | {_fmt(o['core_rev'])} ({_fmt(o['core_rev_yoy'], pct=True)} YoY) |
| Bränsletillägg och övrigt | {_fmt(o['fuel_surcharge_other_rev'])} |
| Amortering kontraktstillgångar | {_fmt(o['amortization_contract_assets'])} |
| **Total omsättning** | **{_fmt(o['total_rev'])}** |
| **Justerad EBITDA** | **{_fmt(o['ebitda_adj_ebitda'])}** |

### Marginalkontroll (justerad EBITDA)

| | Marginal | Just. EBITDA (mkr CAD) |
|---|---:|---:|
| Låg: omsättning × {100 * o.get('margin_low', model.MARGIN_LOW):.0f} % | {100 * o.get('margin_low', model.MARGIN_LOW):.1f} % | {_fmt(o.get('ebitda_at_low'))} |
| **Modellen** | **{_fmt(o.get('model_margin', float('nan')) * 100 if o.get('model_margin') is not None else None)} %** | **{_fmt(o['ebitda_adj_ebitda'])}** |
| Hög: omsättning × {100 * o.get('margin_high', model.MARGIN_HIGH):.0f} % | {100 * o.get('margin_high', model.MARGIN_HIGH):.1f} % | {_fmt(o.get('ebitda_at_high'))} |

Modellens marginal {"ligger inom" if o.get('margin_inside') else "ligger UTANFÖR"} 31–34 %. Historiskt band ur de
{len(o.get('band_quarters', []))} senast rapporterade kvartalen före {q} ({(o.get('band_quarters') or ['–'])[0]}–{(o.get('band_quarters') or ['–'])[-1]}):
{_fmt(100 * o.get('band_low_pre_q', float('nan')))}–{_fmt(100 * o.get('band_high_pre_q', float('nan')))} %. Inga siffror från {q}:s egen rapport används.

EBITDA-brygga från året innan ({_fmt(o['ebitda_base_adj_ebitda'])}): volym {_fmt(o['ebitda_volume'])},
pris/mix {_fmt(o['ebitda_price_mix'])}, kostnadssteg {_fmt(o['ebitda_cost_steps'])} (bl.a. pilotavtalet),
bränslelag {_fmt(o['ebitda_fuel_lag'])} (β={o['fuel_lag_beta']:.3f}, N={o['fuel_lag_n']}, t={o['fuel_lag_t']:.1f}),
drift {_fmt(o['ebitda_drift'])}.

Omsättningsmodell: {fit.kind}{f' (α={fit.alpha:.3g})' if fit.alpha else ''}, N={fit.n}, R²={fit.r2_in_sample:.2f},
CV-RMSE {min(fit.cv_rmse_ols, fit.cv_rmse_ridge):.3f}; β timmar {fit.coef['hours_yoy']:.2f},
β bränsle {fit.coef['fuel_yoy']:.3f}, β USD/CAD {fit.coef['fx_yoy']:.2f}.

## Mot konsensus

{"**Obs:** konsensus är forward-konsensus (" + str(o.get("consensus_note")) + "), inte dagen före rapport." if o.get("consensus_is_forward") else ""}

{cons_block}

*Kvartalet är inte slut: prognosen antar att resten av kvartalet har samma YoY som de uppmätta dagarna.
Ej finansiell rådgivning.*
"""
