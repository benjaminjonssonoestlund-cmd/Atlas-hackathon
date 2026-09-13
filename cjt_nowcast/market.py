"""Makro- och kursdata: jet fuel (EIA), USD/CAD (Bank of Canada), CJT-kurs (stooq).

Kurs: stooq CSV-API (https://stooq.com/q/d/l/?s=…&d1=…&d2=…&i=d&apikey=…).
Kräver API-nyckel sedan 2026 (hämtas via CAPTCHA på stooq.com, läggs i .env
som STOOQ_API_KEY) och symbolen sätts uttryckligen i CJT_STOOQ_SYMBOL —
stooq dokumenterar inte Toronto-suffixet, så det gissas inte. Stooq svarar
HTTP 200 även vid kvot-/nyckelfel och sajten ligger bakom en JS-kontroll;
svaret valideras därför innan det får ligga kvar i cachen.

Jet fuel: EIA:s U.S. Gulf Coast Kerosene-Type Jet Fuel spot FOB ($/gal,
EER_EPJK_PF4_RGC_DPG) — gratis proxy för Platts USGC jet som modellen egentligen
vill ha. Vald eftersom Platts är licensierat.

USD/CAD: Bank of Canada Valet. FXUSDCAD finns från 2017-01-03; 2016 täcks av
den äldre noon-serien IEXE0101 (samma definition: CAD per USD).

Källfilerna sparas som DATERADE ögonblicksbilder i raw/ (serierna växer varje
dag) — senaste cachade används om inte refresh=True, så en körning är
reproducerbar mot exakt de siffror den byggdes på.
"""

from __future__ import annotations

import io
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import xlrd

from .common import BROWSER_UA, PROCESSED, RAW, fetch_cached, quarter_label

EIA_URL = "https://www.eia.gov/dnav/pet/hist_xls/EER_EPJK_PF4_RGC_DPGd.xls"
BOC_URL = ("https://www.bankofcanada.ca/valet/observations/{series}/json"
           "?start_date={start}&end_date={end}")
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}&d1={d1}&d2={d2}&i=d&apikey={key}"
STOOQ_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


class PriceSourceError(RuntimeError):
    """Kurskällan gav inget användbart (nyckel saknas, JS-kontroll, kvot)."""

SERIES_START = "2015-01-01"   # ett år före facit-starten, för YoY 2016
FX_SWITCH = date(2017, 1, 3)
MARKET_CSV = PROCESSED / "market_quarterly.csv"
FUEL_WINDOW_DAYS = 20          # handelsdagar i början/slutet av kvartalet för rörelsemåttet


def _snapshot(subdir: str, stem: str, suffix: str, url: str, refresh: bool,
              headers: dict | None = None) -> Path:
    d = RAW / subdir
    cached = sorted(d.glob(f"{stem}_*{suffix}"))
    if cached and not refresh:
        return cached[-1]
    return fetch_cached(url, d / f"{stem}_{date.today():%Y%m%d}{suffix}",
                        headers=headers, refresh=refresh)


def jet_fuel_daily(refresh: bool = False) -> pd.DataFrame:
    path = _snapshot("eia", "EER_EPJK_PF4_RGC_DPGd", ".xls", EIA_URL, refresh)
    sheet = xlrd.open_workbook(path).sheet_by_name("Data 1")
    rows = []
    for i in range(3, sheet.nrows):  # rad 0–2: rubriker
        serial, price = sheet.row_values(i)[:2]
        if isinstance(serial, float) and isinstance(price, float):
            rows.append((xlrd.xldate_as_datetime(serial, 0).date(), price))
    return pd.DataFrame(rows, columns=["date", "jet_usd_gal"])


def usdcad_daily(refresh: bool = False) -> pd.DataFrame:
    end = date.today().isoformat()
    frames = []
    for series, start, stop in (("IEXE0101", SERIES_START, (FX_SWITCH - timedelta(days=1)).isoformat()),
                                ("FXUSDCAD", FX_SWITCH.isoformat(), end)):
        url = BOC_URL.format(series=series, start=start, end=stop)
        path = _snapshot("boc", series, ".json", url, refresh and series == "FXUSDCAD")
        obs = json.loads(path.read_text())["observations"]
        frames.append(pd.DataFrame(
            [(date.fromisoformat(o["d"]), float(o[series]["v"])) for o in obs
             if o.get(series, {}).get("v") not in (None, "")],
            columns=["date", "usdcad"]))
    return pd.concat(frames, ignore_index=True).drop_duplicates("date").sort_values("date")


def parse_stooq_csv(text: str) -> pd.DataFrame:
    """Stooq-CSV → [date, open, high, low, close, volume]. Kastar vid allt som inte är kursdata."""
    head = text.lstrip()[:200].lower()
    if head.startswith("<") or "javascript" in head:
        raise PriceSourceError("stooq svarade med HTML (JS-kontroll) i stället för CSV")
    df = pd.read_csv(io.StringIO(text))
    if list(df.columns[:6]) != STOOQ_COLUMNS:
        raise PriceSourceError(f"stooq svarade inte med kurs-CSV: {text[:160]!r}")
    df = df.rename(columns=str.lower)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d").dt.date
    df = df.dropna(subset=["close"])
    if df.empty:
        raise PriceSourceError("stooq gav en tom kursserie — fel symbol eller kvot slut?")
    return df[["date", "open", "high", "low", "close", "volume"]].sort_values("date").reset_index(drop=True)


MANUAL_PRICES = Path(__file__).resolve().parent / "manual" / "cjt_prices.csv"


def _number(s) -> float:
    """'1,234.50' / '98.7' / '' → float. Investing.com har tusentalskomma."""
    if pd.isna(s):
        return float("nan")
    s = str(s).replace(",", "").strip()
    return float(s) if s not in ("", "-", "null") else float("nan")


def parse_price_csv(text: str) -> pd.DataFrame:
    """Manuellt nedladdad kurs-CSV → [date, close] (+ open om den finns).

    Känner igen exporterna från Investing.com (Date, Price, Open, High, Low,
    Vol., Change %; datum MM/DD/YYYY, nyast först), Yahoo Finance (Date, Open,
    High, Low, Close, Adj Close, Volume) och StockAnalysis (… Close, Adj. Close …).
    Stängningskursen är 'Close' eller 'Price' — inte justerad, eftersom
    reaktionen mäts över en enda natt och utdelningar inte påverkar den."""
    df = pd.read_csv(io.StringIO(text.lstrip("﻿")))
    cols = {c.strip().lower(): c for c in df.columns}
    close_col = cols.get("close") or cols.get("price")
    if "date" not in cols or close_col is None:
        raise PriceSourceError(f"okänt CSV-format, kolumner: {list(df.columns)}")
    out = pd.DataFrame({"date": pd.to_datetime(df[cols["date"]], format="mixed").dt.date,
                        "close": df[close_col].map(_number)})
    if "open" in cols:
        out["open"] = df[cols["open"]].map(_number)
    out = out.dropna(subset=["date", "close"]).drop_duplicates("date")
    if out.empty:
        raise PriceSourceError("kursfilen innehåller inga rader")
    return out.sort_values("date").reset_index(drop=True)


def prices_daily(refresh: bool = False) -> pd.DataFrame:
    """Daglig CJT-kurs. Manuell fil (manual/cjt_prices.csv) först, annars stooq med nyckel."""
    if MANUAL_PRICES.exists():
        return parse_price_csv(MANUAL_PRICES.read_text(encoding="utf-8", errors="replace"))
    key = os.environ.get("STOOQ_API_KEY", "").strip()
    sym = os.environ.get("CJT_STOOQ_SYMBOL", "").strip()
    if not key or not sym:
        raise PriceSourceError("sätt STOOQ_API_KEY och CJT_STOOQ_SYMBOL i .env (se README)")
    url = STOOQ_URL.format(sym=sym, d1="20150101", d2=f"{date.today():%Y%m%d}", key=key)
    path = _snapshot("stooq", sym.replace(".", "_"), ".csv", url, refresh,
                     {"User-Agent": BROWSER_UA, "Accept": "text/csv,*/*"})
    try:
        return parse_stooq_csv(path.read_text(encoding="utf-8", errors="replace"))
    except PriceSourceError:
        path.unlink(missing_ok=True)   # ett felsvar får aldrig bli "cachad kursdata"
        raise


def quarterly(refresh: bool = False) -> pd.DataFrame:
    jet = jet_fuel_daily(refresh)
    fx = usdcad_daily(refresh)
    df = jet.merge(fx, on="date", how="inner")
    df["jet_cad_gal"] = df["jet_usd_gal"] * df["usdcad"]
    df["quarter"] = df["date"].map(quarter_label)

    def _agg(g: pd.DataFrame) -> pd.Series:
        g = g.sort_values("date")
        head = g["jet_usd_gal"].head(FUEL_WINDOW_DAYS).mean()
        tail = g["jet_usd_gal"].tail(FUEL_WINDOW_DAYS).mean()
        return pd.Series({
            "jet_usd_gal": g["jet_usd_gal"].mean(),
            "usdcad": g["usdcad"].mean(),
            "jet_cad_gal": g["jet_cad_gal"].mean(),
            # Bränslelagen: hur mycket priset rörde sig INOM kvartalet
            "jet_intra_q_move": tail / head - 1 if head else float("nan"),
            "trading_days": len(g),
        })

    out = df.groupby("quarter").apply(_agg, include_groups=False).reset_index()
    MARKET_CSV.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(MARKET_CSV, index=False)
    return out
