"""Konsensus (justerad EPS, omsättning) som den såg ut dagen före rapport.

Källa: manual/consensus.csv — användarens export från Investing.com,
med källa och noter per rad. Skrapning togs bort: uppmätt 2026-09-13 hade
Zacks (CGJTF) ingen konsensus alls för Cargojet och Investing.com blockerade
anrop (403/404).

Seriens regler (från användaren):
- 2024Q1 saknas hos Investing.com. MarketBeats siffror är en annan vendor och
  blandas INTE in → kvartalet ingår inte i träffräkningen.
- 2026Q3 är forward-konsensus per 2026-09-11, inte dagen före rapport.

Kontroller mot facit (`check_actuals`), uppmätt vid inläsning:
- Investing.coms utfallsomsättning = MD&A:s totala omsättning, alla kvartal.
- "actual EPS" 2025Q1 (2,87) är IFRS utspädd EPS, inte justerad (1,62) →
  utfall tas alltid ur MD&A:s adj_eps, aldrig ur konsensusfilen.
- Investing.coms rapportdatum ligger +1 dag mot MD&A-datumet 2025Q1–Q3.
  Kursreaktionen förankras därför i MD&A-datumet (se backtest).
"""

from __future__ import annotations

import pandas as pd

from .common import MANUAL

MANUAL_CSV = MANUAL / "consensus.csv"
COLUMNS = ["quarter", "report_date", "consensus_revenue_cad_m", "consensus_eps_cad",
           "consensus_adj_ebitda_cad_m", "actual_revenue_cad_m", "actual_eps_cad", "source", "notes"]
NUMERIC = ["consensus_revenue_cad_m", "consensus_eps_cad", "consensus_adj_ebitda_cad_m",
           "actual_revenue_cad_m", "actual_eps_cad"]


def load() -> pd.DataFrame:
    df = pd.read_csv(MANUAL_CSV, dtype=str, keep_default_na=False)
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{MANUAL_CSV.name} saknar kolumner: {sorted(missing)}")
    for col in NUMERIC:
        df[col] = pd.to_numeric(df[col].replace("", None), errors="coerce")
    df["report_date"] = df["report_date"].replace("", None)
    # Namn som modellen/backtestet använder
    df["consensus_revenue_mcad"] = df["consensus_revenue_cad_m"]
    df["consensus_eps_adj_cad"] = df["consensus_eps_cad"]
    df["consensus_adj_ebitda_mcad"] = df["consensus_adj_ebitda_cad_m"]
    df["is_forward"] = df["notes"].str.contains("forward", case=False)
    return df


def check_actuals(cons: pd.DataFrame, mda: pd.DataFrame) -> pd.DataFrame:
    """Konsensusfilens utfall och datum mot MD&A — avvikelser, inte tysta ersättningar."""
    d = cons.merge(mda[["quarter", "mda_date", "total_rev", "adj_eps", "eps_diluted"]], on="quarter", how="left")
    d = d[d["actual_revenue_cad_m"].notna() | d["actual_eps_cad"].notna()]
    d["rev_diff"] = d["actual_revenue_cad_m"] - d["total_rev"]
    d["eps_diff_vs_adj"] = d["actual_eps_cad"] - d["adj_eps"]
    d["eps_is_ifrs_diluted"] = (d["eps_diff_vs_adj"].abs() > 0.005) & \
        ((d["actual_eps_cad"] - d["eps_diluted"]).abs() <= 0.005)
    d["date_diff_days"] = (pd.to_datetime(d["report_date"]) - pd.to_datetime(d["mda_date"])).dt.days
    bad = (d["rev_diff"].abs() > 0.05) | (d["eps_diff_vs_adj"].abs() > 0.005) | (d["date_diff_days"].abs() > 0)
    return d.loc[bad, ["quarter", "report_date", "mda_date", "date_diff_days", "actual_revenue_cad_m",
                       "total_rev", "rev_diff", "actual_eps_cad", "adj_eps", "eps_diluted", "eps_is_ifrs_diluted"]]
