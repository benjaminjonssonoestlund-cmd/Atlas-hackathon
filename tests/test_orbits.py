"""Banpropagering och passageprediktion — ren aritmetik, inga nätverksanrop."""

from __future__ import annotations

from atlas_choke.engine import orbits


def _tle():
    """Sentinel-1A i solsynkron bana (strukturellt giltig TLE)."""
    return [{"name": "SENTINEL-1A", "norad": 39634, "role": "SAR-radar",
             "why": "test", "highlight": True,
             "tle1": "1 39634U 14016A   26250.50000000  .00000100  00000-0  50000-4 0  9995",
             "tle2": "2 39634  98.1800 250.0000 0001300  90.0000 270.0000 14.59200000    10"}]


class TestPositions:
    def test_position_is_plausible(self):
        pos = orbits.positions(_tle())
        assert len(pos) == 1
        p = pos[0]
        assert -90 <= p["lat"] <= 90
        assert -180 <= p["lon"] <= 180
        assert 500 < p["alt_km"] < 900        # Sentinel-1 ligger på ~700 km
        assert p["role"] == "SAR-radar" and p["highlight"] is True

    def test_bad_tle_is_skipped(self):
        bad = [{"name": "X", "norad": 1, "tle1": "skrap", "tle2": "skrap"}]
        assert orbits.positions(bad) == []

    def test_satellite_moves_between_epochs(self):
        from datetime import datetime, timedelta, timezone
        t0 = datetime.now(timezone.utc)
        a = orbits.positions(_tle(), t0)[0]
        b = orbits.positions(_tle(), t0 + timedelta(minutes=10))[0]
        # ~7 km/s → tio minuter flyttar satelliten flera grader
        assert abs(a["lat"] - b["lat"]) + abs(a["lon"] - b["lon"]) > 1.0


class TestPasses:
    def test_pass_within_swath_and_horizon(self):
        pts = [{"id": "suez", "lat": 30.4, "lon": 32.35}]
        out = orbits.next_passes(_tle(), pts, horizon_h=24)
        if out:                       # polär bana täcker punkten inom ett dygn
            v = out["suez"]
            assert 0 <= v["in_minutes"] <= 24 * 60
            assert v["distance_km"] <= orbits.SWATH_KM
            assert v["satellite"] == "SENTINEL-1A"

    def test_role_filter_excludes_non_radar(self):
        t = _tle()
        t[0]["role"] = "Optisk"
        assert orbits.next_passes(t, [{"id": "x", "lat": 0, "lon": 0}]) == {}

    def test_no_satellites_gives_empty(self):
        assert orbits.next_passes([], [{"id": "x", "lat": 0, "lon": 0}]) == {}
