"""Cargojet tracker — historik ur backtesten och flygtimmar hittills. Inga nätverksanrop."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta

import pytest

from atlas_choke.sources import cjt_tracker
from atlas_choke.sources.cargojet import _points, airborne_intervals
from atlas_choke.sources.cjt_tracker import ET

HEADER = ["kvartal", "rapportdag", "timmar", "mått", "prognos", "konsensus",
          "prognos_vs_konsensus", "utfall", "utfall_vs_konsensus", "rätt", "kurs_rapportdag_pct"]


def _write_surprise(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)


class TestHistory:
    def test_revenue_rows_with_outcome_newest_first(self, tmp_path):
        path = tmp_path / "s.csv"
        _write_surprise(path, [
            ["2024Q2", "2024-08-13", "x", "Omsättning", "230.4", "234.13", "UNDER", "230.8", "UNDER", "True", "2.5"],
            ["2024Q2", "2024-08-13", "x", "Just. EPS", "0.9", "0.9", "ÖVER", "-0.05", "UNDER", "False", "2.5"],
            ["2024Q4", "2025-02-17", "x", "Omsättning", "264.4", "272.83", "UNDER", "293.2", "ÖVER", "False", "2.1"],
            ["2026Q3", "2026-11-10", "x", "Omsättning", "252.5", "265.36", "UNDER", "", "–", "", ""],
        ])
        items = cjt_tracker.load_history(path)
        assert [it["id"] for it in items] == ["2024Q4", "2024Q2"]
        q4 = items[0]
        assert (q4["quarter"], q4["year"], q4["signal"], q4["correct"]) == ("Q4", "2024", "LOWER", False)
        assert q4["consensus_mcad"] == pytest.approx(272.83) and q4["result_mcad"] == pytest.approx(293.2)
        assert items[1]["signal"] == "LOWER" and items[1]["correct"] is True

    @pytest.mark.skipif(not cjt_tracker.SURPRISE_CSV.exists(), reason="backtesten är inte körd lokalt")
    def test_real_backtest_has_nine_quarters(self):
        items = cjt_tracker.load_history()
        assert len(items) == 9
        assert all(it["signal"] in ("HIGHER", "LOWER") for it in items)


class TestHours:
    def test_merge_joins_midnight_split_and_drops_taxi(self):
        merged = cjt_tracker.merge_intervals([[0, 3600, 30000], [3700, 7200, 31000], [20000, 20300, 900]])
        assert merged == [[0, 7200, 31000]]

    def test_airborne_intervals_from_trace(self):
        trace = [[0, 43.17, -79.93, "ground", 10, 90, 0, None, None]]
        trace += [[60 + i * 60, 43.17 + i * 0.01, -79.93, 30000, 450, 280, 0, 0, None] for i in range(120)]
        trace += [[60 + 121 * 60, 44.4, -79.93, "ground", 10, 280, 0, None, None]]
        iv = airborne_intervals(_points({"timestamp": 1_780_000_000, "trace": trace}))
        assert len(iv) == 1 and iv[0][1] - iv[0][0] == 119 * 60 and iv[0][2] == 30000

    @staticmethod
    def _fleet_flying(start: datetime, days: int, skip: set[int] = frozenset(), n_craft: int = 6,
                      hours_per_flight: float = 2.0) -> dict[str, list[list]]:
        """n_craft plan som flyger en flygning kl 10 ET varje dygn, utom dygnen i `skip`."""
        out = {f"c{i:05d}": [] for i in range(n_craft)}
        for d in range(days):
            if d in skip:
                continue
            t0 = (start + timedelta(days=d, hours=10)).timestamp()
            for hexid in out:
                out[hexid].append([t0, t0 + hours_per_flight * 3600, 35000])
        return out

    def test_uncovered_day_is_estimated_from_same_weekday(self):
        start = datetime(2026, 7, 1, tzinfo=ET)
        end = start + timedelta(days=14)                     # 15 dygn, sista pågår 00:00 → 0 h
        iv = self._fleet_flying(start, 14, skip={3})
        w = cjt_tracker.window_estimate(iv, start, end)
        assert w["days"] == 15 and w["covered_days"] == 13
        assert w["measured_hours"] == pytest.approx(13 * 12)
        assert w["hours"] == pytest.approx(14 * 12)          # dygn 3 skattat med samma veckodag (dygn 10)

    def test_quarter_to_date_compares_same_window_prior_years(self):
        now = datetime(2026, 7, 11, 10, 0, tzinfo=ET)
        seen = []

        def provider(s, e):
            seen.append((s.date().isoformat(), e.date().isoformat()))
            factor = 1.0 if s.year == 2026 else 0.5          # i år flyger man dubbelt så länge
            return self._fleet_flying(s, 10, hours_per_flight=2.0 * factor)

        out = cjt_tracker.quarter_to_date(now, provider)
        assert (out["quarter"], out["year"]) == ("Q3", "2026")
        assert seen == [("2026-07-01", "2026-07-11"), ("2025-07-01", "2025-07-11"), ("2024-07-01", "2024-07-11")]
        assert [c["year"] for c in out["comparisons"]] == ["2025", "2024"]
        assert out["level"] == "HIGH"
        assert out["hours"] > out["comparisons"][0]["hours"]

    def test_no_data_gives_no_hours(self):
        start = datetime(2026, 7, 1, tzinfo=ET)
        w = cjt_tracker.window_estimate({"c00001": []}, start, start + timedelta(days=3))
        assert w["hours"] is None and w["coverage"] == 0
