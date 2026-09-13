"""Kommandorad för hela kedjan. Varje steg läser cachat råmaterial när det finns.

    python -m cjt_nowcast fleet                 # TC-registret → fleet.csv
    python -m cjt_nowcast extract [--workers 4] # ADS-B-dagsdumpar → Cargojet-spår (tar ~1 dygn första gången)
    python -m cjt_nowcast health                # täckning per kvartal + upptäckta plan
    python -m cjt_nowcast flights               # spår → flygningar med blocktid
    python -m cjt_nowcast routes                # ruttabell (för klassbeslut) + kvartal × segment
    python -m cjt_nowcast mda                   # MD&A 2016–2026 → mda_quarterly.csv
    python -m cjt_nowcast market [--refresh]    # jet fuel, USD/CAD, CJT.TO
    python -m cjt_nowcast consensus             # manual/consensus.csv + skrapat
    python -m cjt_nowcast validate              # ADS-B-blocktimmar mot rapporterade
    python -m cjt_nowcast backtest              # 2024Q1–2026Q2 mot utfall och konsensus
    python -m cjt_nowcast nowcast               # 2026Q3: timmar per segment, prognos, konsensus
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .common import quarter_bounds, quarters_between

STUDY_FIRST = "2024Q1"
# Hämtordning: det valideringen och Q3-nowcasten behöver först
PRIORITY = ["2026Q2", "2025Q2", "2026Q3", "2025Q3"]


def study_days(priority: bool = True) -> list[date]:
    last = date.today() - timedelta(days=1)
    last_q = f"{last.year}Q{(last.month - 1) // 3 + 1}"
    qs = quarters_between(STUDY_FIRST, last_q)
    if priority:
        qs = [q for q in PRIORITY if q in qs] + [q for q in reversed(qs) if q not in PRIORITY]
    days = []
    for q in qs:
        s, e = quarter_bounds(q)
        e = min(e, last)
        days += [s + timedelta(i) for i in range((e - s).days + 1)]
    return days


def cmd_fleet(args) -> None:
    from . import fleet
    f = fleet.build(refresh=args.refresh)
    print(f.groupby("type").size().to_string())
    print(f"{len(f)} flygplan")


def cmd_extract(args) -> None:
    from . import adsb, fleet
    res = adsb.extract_days(study_days(), set(fleet.load()["hex"]), workers=args.workers)
    print(Counter(r.get("status") for r in res))
    for r in res:
        if r.get("status") != "done":
            print("  ", r)


def cmd_health(args) -> None:
    from . import adsb, fleet
    days = sorted(study_days(priority=False))
    h = adsb.day_health(days)
    h["quarter"] = [f"{d.year}Q{(d.month - 1) // 3 + 1}" for d in h["day"]]
    print(h.groupby("quarter").agg(days=("day", "size"), done=("done", "sum"),
                                   cargojet_aircraft_mean=("cargojet_aircraft", "mean")).round(1).to_string())
    disc = adsb.discovered_aircraft(days)
    if not disc.empty:
        known = set(fleet.load()["hex"])
        disc["in_registry"] = disc["hex"].isin(known)
        print("\nUpptäckta plan som INTE finns i TC-registrets Cargojet-lista:")
        print(disc[~disc["in_registry"]].to_string(index=False))


def cmd_flights(args) -> None:
    from . import adsb, flights
    days = sorted(study_days(priority=False))
    disc = adsb.discovered_aircraft(days)
    # Flygningar byggs för alla plan som flugit med CJT-callsign eller ägs av Cargojet
    fleet_df = disc[disc["days_cjt_callsign"].gt(0) | disc["ownOp"].str.contains("argojet", na=False)]
    fleet_df = fleet_df.rename(columns={"icao_type": "type"})
    df, info = flights.build(fleet_df, days, workers=args.workers)
    print(info)
    print(df.groupby(["quarter", "quality"])["block_h"].sum().unstack().round(0).to_string())


def cmd_routes(args) -> None:
    from . import flights, routes
    f = flights.load()
    t = routes.route_table(f)
    pd.set_option("display.width", 220)
    print(t.head(args.top).to_string(index=False))
    print(f"\n{len(t)} rutter → {routes.ROUTE_TABLE}")
    q = routes.quarterly_segments(f)
    print(q.pivot(index="quarter", columns="segment", values="block_h").round(0).to_string())


def cmd_mda(args) -> None:
    from . import mda
    mda.build()


def cmd_market(args) -> None:
    from . import market
    print(market.quarterly(refresh=args.refresh).round(3).to_string(index=False))


def cmd_consensus(args) -> None:
    from . import consensus, validate
    c = consensus.load()
    pd.set_option("display.width", 220)
    print(c[consensus.COLUMNS].to_string(index=False))
    mda = pd.read_csv(validate.PROCESSED / "mda_quarterly.csv")
    bad = consensus.check_actuals(c, mda)
    print("\nAvvikelser mot MD&A (utfall tas alltid ur MD&A):")
    print(bad.to_string(index=False) if len(bad) else "inga")


def cmd_validate(args) -> None:
    from . import flights, validate
    mda = pd.read_csv(validate.PROCESSED / "mda_quarterly.csv")
    t = validate.block_hours_table(flights.load(), mda)
    pd.set_option("display.width", 220)
    print(t.round(3).to_string(index=False))
    print(validate.summary(t))


def cmd_backtest(args) -> None:
    """Historiken: standard = rapporterade blocktimmar som indata (de 9 testerna).
    --hours adsb kör samma test med ADS-B-mätta timmar — först när mätningen är validerad."""
    from . import backtest
    if args.hours == "reported":
        backtest.surprise_table(args.first, args.last)
        print(backtest.SURPRISE_MD.read_text(encoding="utf-8"))
        return
    df = backtest.run_backtest(args.first, args.last)
    pd.set_option("display.width", 250)
    cols = [c for c in ("quarter", "hours_method", "hours_yoy", "reported_hours_yoy", "err_total_rev_pct",
                        "err_adj_ebitda_pct", "perfect_hours_err_total_rev_pct", "hit_rev", "hit_eps",
                        "move_pct", "error") if c in df]
    print(df[cols].round(3).to_string(index=False))
    print(backtest.backtest_summary(df))


def cmd_nowcast(args) -> None:
    from . import backtest
    backtest.run_nowcast(args.quarter)
    print(open(str(backtest.NOWCAST_MD).format(q=args.quarter), encoding="utf-8").read())


# Stickprov för 2026Q3: en dag per veckodag spridd över kvartalet (inga helgdagar,
# inte heller 364 dagar tidigare). Matchas mot samma veckodag året innan.
SAMPLE_2026Q3 = [date(2026, 7, 14), date(2026, 7, 23), date(2026, 8, 5), date(2026, 8, 15),
                 date(2026, 8, 21), date(2026, 8, 23), date(2026, 8, 24),
                 date(2026, 7, 29), date(2026, 8, 11), date(2026, 9, 3), date(2026, 9, 10)]
SAMPLE_2026Q2 = [date(2026, 4, 1) + timedelta(i) for i in range(17)]


def cmd_sample(args) -> None:
    """Snabbläge: validering (april) + nowcast (Q3) på veckodagsmatchade stickprovsdagar."""
    from . import adsb, backtest, flights, model, routes
    lag = timedelta(days=backtest.LAG_SAME_WEEKDAY)
    wanted = sorted({*SAMPLE_2026Q3, *SAMPLE_2026Q2, *(d - lag for d in SAMPLE_2026Q3 + SAMPLE_2026Q2)})
    done = [d for d in wanted if adsb.is_day_done(d)]
    print(f"Hämtade stickprovsdagar: {len(done)} av {len(wanted)}")
    disc = adsb.discovered_aircraft(done)
    fleet_df = disc[disc["days_cjt_callsign"].gt(0) | disc["ownOp"].str.contains("argojet", na=False)]
    fleet_df = fleet_df.rename(columns={"icao_type": "type"})
    df, info = flights.build_sampled(fleet_df, done)
    print(f"{len(df)} flygningar på {fleet_df['hex'].nunique()} plan | {info}")

    pd.set_option("display.width", 220)
    t = routes.route_table(df)
    print(f"\nRuttabell ({len(t)} rutter) → {routes.ROUTE_TABLE}")
    print(t[["dep", "arr", "flights", "block_h", "night_share", "suggested", "segment"]].head(25).to_string(index=False))

    seg = routes.with_segments(df)
    panel = model.load_panel(last="2026Q3")
    v = backtest.matched_day_yoy(seg, SAMPLE_2026Q2, panel, "2026Q2")
    rep = panel.at["2026Q2", "block_hours"] / panel.at["2026Q2", "py_block_hours"] - 1
    level = backtest.weekday_scaled_hours(seg, SAMPLE_2026Q2, "2026Q2")
    print(f"\nVALIDERING (april 2026 mot samma veckodagar 2025, {v['n_pairs']} dagpar):")
    print(f"  ADS-B blocktimmar YoY {100 * v.get('hours_yoy_total', float('nan')):+.1f} %  "
          f"(cykler {100 * v['cycles_yoy']:+.1f} %)  |  rapporterat Q2 2026 YoY {100 * rep:+.1f} %")
    print(f"  Nivå: veckodagsuppskalat Q2 2026 = {level:,.0f} h mot rapporterade "
          f"{panel.at['2026Q2', 'block_hours']:,.0f} h ({100 * (level / panel.at['2026Q2', 'block_hours'] - 1):+.1f} %)")

    out = backtest.run_nowcast_sampled("2026Q3", SAMPLE_2026Q3)
    print("\n" + open(str(backtest.NOWCAST_MD).format(q="2026Q3"), encoding="utf-8").read())


def _fleet_from_manifests(days: list[date]) -> pd.DataFrame:
    from . import adsb
    disc = adsb.discovered_aircraft(days)
    if disc.empty:
        return pd.DataFrame(columns=["hex", "registration", "type"])
    f = disc[disc["days_cjt_callsign"].gt(0) | disc["ownOp"].str.contains("argojet", na=False)]
    return f.rename(columns={"icao_type": "type"})


def cmd_level(args) -> None:
    """Nivåkontroll: ADS-B-blocktimmar (veckodagsuppskalade) mot rapporterade, per kvartal."""
    from . import adsb, backtest, flights, model
    panel = model.load_panel(last="2026Q3")
    rows = []
    for q in args.quarters.split(","):
        s, e = quarter_bounds(q)
        done = [s + timedelta(i) for i in range((e - s).days + 1) if adsb.is_day_done(s + timedelta(i))]
        if not done:
            print(f"{q}: inga hämtade dagar")
            continue
        df, _ = flights.build_sampled(_fleet_from_manifests(done), done, stitch_consecutive=True,
                                      out_csv=flights.PROCESSED / f"flights_level_{q}.csv.gz")
        ok = df[~df["glitch"].astype(bool)]
        est = backtest.weekday_scaled_hours(ok, done, q)
        rep = panel.at[q, "block_hours"]
        rows.append({"kvartal": q, "dagar": len(done), "veckodagar": len({d.weekday() for d in done}),
                     "adsb_uppskalat_h": round(est), "rapporterat_h": round(rep),
                     "kvot_rapp_per_adsb": round(rep / est, 3), "avvikelse_%": round(100 * (est / rep - 1), 1),
                     "andel_rundturer": round(ok.loc[ok["quality"] == "roundtrip_inferred", "block_h"].sum() / ok["block_h"].sum(), 3)})
    t = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(t.to_string(index=False))
    if len(t) >= 2:
        k = t["kvot_rapp_per_adsb"]
        print(f"\nKalibreringskvot rapporterat/ADS-B: {k.min():.3f}–{k.max():.3f} "
              f"(spann {100 * (k.max() / k.min() - 1):.1f} %). Stabil kvot = nivåfelet går att kalibrera bort.")
    t.to_csv(flights.PROCESSED / "level_check.csv", index=False)


def _ledger_update(d: date, ledger_path: Path) -> dict:
    """Blocktimmar för en dag → rad i dagsfilen (ersätter ev. tidigare rad för dagen)."""
    from . import adsb, flights, routes
    m = adsb.day_manifest(d)
    fl = _fleet_from_manifests([d])
    df, _ = flights.build_sampled(fl, [d], out_csv=flights.PROCESSED / "track" / f"flights_{d:%Y%m%d}.csv.gz") \
        if len(fl) else (pd.DataFrame(), {})
    ok = routes.with_segments(df[~df["glitch"].astype(bool)]) if len(df) else pd.DataFrame(
        columns=["segment", "block_h", "cycles", "quality"])
    row = {"day": d.isoformat(), "weekday": d.strftime("%a"), "source": m.get("source", "dump"),
           "aircraft": int(len(fl)), "flights": int(len(ok)), "cycles": int(ok["cycles"].sum()),
           "block_h": round(float(ok["block_h"].sum()), 2),
           **{f"block_h_{s}": round(float(ok.loc[ok["segment"] == s, "block_h"].sum()), 2)
              for s in ("domestic", "acmi", "charter")},
           "roundtrip_share": round(float(ok.loc[ok["quality"] == "roundtrip_inferred", "block_h"].sum()
                                          / max(ok["block_h"].sum(), 1e-9)), 3),
           "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ledger = pd.read_csv(ledger_path, dtype={"day": str}) if ledger_path.exists() else pd.DataFrame()
    if len(ledger):
        ledger = ledger[ledger["day"] != row["day"]]
    ledger = pd.concat([ledger, pd.DataFrame([row])], ignore_index=True).sort_values("day")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(ledger_path, index=False)
    return row


def cmd_track(args) -> None:
    """Löpande insamling: nya dagar först när de publiceras, sedan bakåt genom kvartalet.

    Sparar blocktimmar per dag och segment i data/processed/block_hours_daily_<kvartal>.csv.
    Stickprovsdagarna prioriteras. Sover när inget nytt finns; avslutas när kvartalet är
    slut och alla dagar är hämtade."""
    import json as _json
    import time as _time
    from . import adsb, fleet
    from .common import PROCESSED as _P, RAW as _R
    q = args.quarter
    s, e = quarter_bounds(q)
    ledger_path = _P / f"block_hours_daily_{q}.csv"
    priority = [d for d in SAMPLE_2026Q3 if s <= d <= e]
    while True:
        hexes = set(fleet.load()["hex"])
        for p in (_R / "adsb" / "days").glob("*.json"):
            hexes |= {a["hex"] for a in _json.loads(p.read_text())["aircraft"] if a["hex"]}
        last = min(e, date.today() - timedelta(days=1))
        qdays = [s + timedelta(i) for i in range((last - s).days + 1)]
        have = set(pd.read_csv(ledger_path, dtype={"day": str})["day"]) if ledger_path.exists() else set()
        for d in qdays:
            if adsb.is_day_done(d) and d.isoformat() not in have:
                print("dagsfil:", _ledger_update(d, ledger_path), flush=True)
        todo = [d for d in priority if not adsb.is_day_done(d)] + \
               [d for d in reversed(qdays) if not adsb.is_day_done(d) and d not in priority]
        progressed = False
        for d in todo:
            try:
                m = adsb.extract_day(d, hexes)
            except Exception as exc:  # noqa: BLE001 — en dag får inte stoppa insamlingen
                print(d, "FEL", repr(exc)[:200], flush=True)
                continue
            if m.get("status") == "done":
                print("dagsfil:", _ledger_update(d, ledger_path), flush=True)
                progressed = True
                break          # räkna om prioriteringen efter varje dag (nya releaser går först)
        if not progressed:
            if date.today() > e + timedelta(days=3):
                print("kvartalet är slut och alla publicerade dagar hämtade", flush=True)
                return
            _time.sleep(args.sleep)


def cmd_ui(args) -> None:
    """Omsättnings-UI:t (andra UI:t, parallellt med Atlas-trackern på 8060)."""
    from .ui import app as ui_app
    ui_app.main(args.host, args.port)


def cmd_surprise(args) -> None:
    """Prognos ÖVER/UNDER konsensus → utfall → rätt? (kärnutdata)."""
    from . import backtest
    backtest.surprise_table()
    print(backtest.SURPRISE_MD.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m cjt_nowcast", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("fleet"); s.add_argument("--refresh", action="store_true"); s.set_defaults(fn=cmd_fleet)
    s = sub.add_parser("extract"); s.add_argument("--workers", type=int, default=4); s.set_defaults(fn=cmd_extract)
    s = sub.add_parser("health"); s.set_defaults(fn=cmd_health)
    s = sub.add_parser("flights"); s.add_argument("--workers", type=int, default=6); s.set_defaults(fn=cmd_flights)
    s = sub.add_parser("routes"); s.add_argument("--top", type=int, default=60); s.set_defaults(fn=cmd_routes)
    s = sub.add_parser("mda"); s.set_defaults(fn=cmd_mda)
    s = sub.add_parser("market"); s.add_argument("--refresh", action="store_true"); s.set_defaults(fn=cmd_market)
    s = sub.add_parser("consensus"); s.set_defaults(fn=cmd_consensus)
    s = sub.add_parser("validate"); s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("backtest"); s.add_argument("--first", default="2024Q2")
    s.add_argument("--last", default="2026Q2")
    s.add_argument("--hours", choices=["reported", "adsb"], default="reported"); s.set_defaults(fn=cmd_backtest)
    s = sub.add_parser("nowcast"); s.add_argument("--quarter", default="2026Q3"); s.set_defaults(fn=cmd_nowcast)
    s = sub.add_parser("sample"); s.set_defaults(fn=cmd_sample)
    s = sub.add_parser("surprise"); s.set_defaults(fn=cmd_surprise)
    s = sub.add_parser("level"); s.add_argument("--quarters", default="2025Q2,2025Q3,2026Q2"); s.set_defaults(fn=cmd_level)
    s = sub.add_parser("ui"); s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8061); s.set_defaults(fn=cmd_ui)
    s = sub.add_parser("track"); s.add_argument("--quarter", default="2026Q3")
    s.add_argument("--sleep", type=int, default=3600); s.set_defaults(fn=cmd_track)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    args.fn(args)


if __name__ == "__main__":
    main()
