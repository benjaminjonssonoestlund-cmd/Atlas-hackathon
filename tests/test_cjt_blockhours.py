"""Cargojet tracker — blocktimmar, ledger, kalibrering och rapporterat pro rata. Inga nätverksanrop."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta

import pytest

from atlas_choke.sources import cjt_blockhours as B
from atlas_choke.sources.cjt_tracker import ET


def _fleet(start: datetime, days: int, n_craft: int = 6, hours: float = 2.0,
           skip: set[int] = frozenset()) -> dict[str, list[list]]:
    """n_craft plan, en flygning kl 10 ET per dygn utom dygnen i `skip`."""
    out = {f"c{i:05d}": [] for i in range(n_craft)}
    for d in range(days):
        if d in skip:
            continue
        t0 = (start + timedelta(days=d, hours=10)).timestamp()
        for iv in out.values():
            iv.append([t0, t0 + hours * 3600, 35000])
    return out


class TestReported:
    def test_reads_mda_table_and_skips_blanks(self, tmp_path):
        path = tmp_path / "mda.csv"
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["quarter", "revenue", "block_hours"])
            w.writerow(["2025Q3", "240", "15861"])
            w.writerow(["2026Q3", "", ""])
        rep = B.load_reported(path)
        assert rep["2025Q3"] == 15861 and "2026Q3" not in rep
        assert rep["2024Q3"] == 18928                               # fallback

    @pytest.mark.skipif(not B.MDA_CSV.exists(), reason="MD&A-tabellen är inte byggd lokalt")
    def test_real_reports_q3(self):
        rep = B.load_reported()
        assert rep["2025Q3"] == 15861 and rep["2024Q3"] == 18928


class TestMeasurement:
    def test_day_stats_adds_taxi_per_flight(self):
        start = datetime(2026, 7, 1, tzinfo=ET)
        stats = B.day_stats(_fleet(start, 2), start, start + timedelta(days=2))
        s = stats[date(2026, 7, 1)]
        assert (s["flights"], s["aircraft"]) == (6, 6) and s["airborne_h"] == pytest.approx(12)
        assert B.block_h(s) == pytest.approx(12 + 6 * B.TAXI_H_PER_FLIGHT)

    def test_estimate_fills_missing_day_with_same_weekday(self):
        days = [date(2026, 7, 1) + timedelta(days=i) for i in range(14)]
        measured = {d: 10.0 + d.weekday() for d in days if d != days[3]}
        r = B.estimate_total(measured, days, measured)
        assert r["estimated"] == pytest.approx(10.0 + days[3].weekday())
        assert r["total"] == pytest.approx(sum(10.0 + d.weekday() for d in days))

    def test_no_reference_estimates_nothing(self):
        days = [date(2026, 7, 1), date(2026, 7, 2)]
        assert B.estimate_total({}, days, {}) == {"total": 0.0, "measured": 0.0, "estimated": 0.0}


class TestLedger:
    def test_persists_and_never_decreases(self, tmp_path):
        path = tmp_path / "ledger.json"
        start = datetime(2026, 7, 1, tzinfo=ET)
        now = start + timedelta(days=5, hours=12)
        full = _fleet(start, 6)
        B.update_ledger(B.Ledger(path), now, lambda s, e: full, lambda d: True)

        # Spåren för de första dygnen rensas — ledgern ska behålla timmarna
        pruned = _fleet(start, 6, skip={0, 1})
        ledger = B.Ledger(path)
        B.update_ledger(ledger, now, lambda s, e: pruned, lambda d: True)
        again = B.Ledger(path)
        assert again.doc["days"]["2026-07-01"]["airborne_h"] == pytest.approx(12)
        assert again.doc["days"]["2026-07-01"]["complete"] is True
        assert again.doc["days"]["2026-07-06"]["complete"] is False      # pågående dygn
        assert again.doc["days"]["2026-07-02"]["final"] is True          # ≥ 4 dygn gammalt

    def test_growing_day_is_updated(self, tmp_path):
        ledger = B.Ledger(tmp_path / "l.json")
        d = date(2026, 7, 1)
        ledger.merge_day(d, {"airborne_h": 5.0, "flights": 2, "aircraft": 2}, False, False, 0)
        ledger.merge_day(d, {"airborne_h": 9.0, "flights": 4, "aircraft": 4}, False, False, 1)
        assert ledger.doc["days"]["2026-07-01"]["flights"] == 4


class TestCalibration:
    def test_median_ratio(self):
        cal = B.calibration_factor({"a": 110, "b": 120, "c": 150}, {"a": 100, "b": 100, "c": 100, "d": 50})
        assert cal["factor"] == pytest.approx(1.2) and set(cal["ratios"]) == {"a", "b", "c"}

    def test_quarter_estimate_needs_enough_complete_days(self):
        start = B.T.quarter_start(2025, 3)
        iv = _fleet(start, 92)
        complete_days = {start.date() + timedelta(days=i) for i in range(10)}
        est = B.adsb_quarter_estimate(lambda s, e: iv, lambda d: d in complete_days, 2025, 3)
        per_day = 12 + 6 * B.TAXI_H_PER_FLIGHT
        assert est["days_used"] == 10 and est["total"] == pytest.approx(92 * per_day)
        assert B.adsb_quarter_estimate(lambda s, e: iv, lambda d: False, 2025, 3) is None


class TestQuarterToDate:
    def test_calibrated_hours_vs_reports_pro_rata(self, tmp_path):
        start = datetime(2026, 7, 1, tzinfo=ET)
        now = datetime(2026, 8, 15, 12, 0, tzinfo=ET)        # mitt i kvartalet
        iv = _fleet(start, 46, skip={10})
        ledger = B.Ledger(tmp_path / "l.json")
        B.update_ledger(ledger, now, lambda s, e: iv, lambda d: True)
        reported = {"2025Q3": 15861.0, "2024Q3": 18928.0}
        out = B.quarter_to_date(now, ledger.doc, reported, {"factor": 1.1})

        elapsed = (now - start) / (datetime(2026, 10, 1, tzinfo=ET) - start)
        assert out["elapsed_fraction"] == pytest.approx(elapsed, abs=1e-4)
        assert [c["year"] for c in out["comparisons"]] == ["2025", "2024"]
        assert out["comparisons"][0]["hours"] == round(15861 * elapsed)
        assert out["comparisons"][1]["reported_quarter_hours"] == 18928

        per_day = 12 + 6 * B.TAXI_H_PER_FLIGHT
        # 45 hela dygn (dygn 10 skattat) + pågående dygn: flygningen kl 10–12 är klar
        assert out["adsb_block_hours"] == pytest.approx(46 * per_day, rel=1e-3)
        assert out["hours"] == round(out["adsb_block_hours"] * 1.1)
        assert out["covered_days"] == 45 and out["days"] == 46
        assert out["level"] == ("HIGH" if out["hours"] >= out["comparisons"][0]["hours"] else "LOW")

    def test_empty_ledger_gives_no_hours(self):
        now = datetime(2026, 7, 3, tzinfo=ET)
        out = B.quarter_to_date(now, {"days": {}}, {"2025Q3": 15861.0}, None)
        assert out["hours"] is None and out["level"] is None
        assert out["comparisons"][0]["hours"] is not None
