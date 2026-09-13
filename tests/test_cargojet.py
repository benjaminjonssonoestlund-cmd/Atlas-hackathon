"""Cargojet-segmentering — syntetiska spår, inga nätverksanrop."""

from __future__ import annotations

from atlas_choke.sources.cargojet import Airports, _points, segment

AIRPORTS = Airports([
    ["YHM", "CYHM", "Hamilton John C. Munro", 43.1736, -79.935],
    ["YVR", "CYVR", "Vancouver International", 49.1939, -123.184],
])


def _flight(t0: float, lat0: float, lon0: float, lat1: float, lon1: float,
            minutes: int = 240, callsign: str = "CJT551") -> list[list]:
    """readsb-liknande trace: markpunkter, stigning, marsch, sjunk, markpunkter."""
    trace = [[t0 - 120, lat0, lon0, "ground", 10, 90, 0, None, None]]
    n = minutes
    for i in range(n + 1):
        f = i / n
        alt = min(35000, 600 * min(i, n - i) + 500)
        detail = {"flight": callsign + "  "} if i % 30 == 0 else None
        trace.append([t0 + i * 60, lat0 + (lat1 - lat0) * f, lon0 + (lon1 - lon0) * f,
                      alt, 450, 280, 0, 0, detail])
    trace.append([t0 + n * 60 + 120, lat1, lon1, "ground", 12, 280, 0, None, None])
    return trace


def _doc(trace: list[list], ts: float = 1_789_000_000.0) -> dict:
    return {"icao": "c08412", "timestamp": ts, "trace": trace}


class TestSegment:
    def test_one_flight_with_airports_and_callsign(self):
        doc = _doc(_flight(0, 43.17, -79.93, 49.19, -123.18))
        flights = segment(_points(doc), AIRPORTS)
        assert len(flights) == 1
        f = flights[0]
        assert f["callsign"] == "CJT551"
        assert f["dep"]["iata"] == "YHM" and f["arr"]["iata"] == "YVR"
        assert 235 <= f["dur_min"] <= 245
        lon, lat, alt_m, t, track = f["path"][0]
        assert abs(lat - 43.17) < 0.01 and t == int(1_789_000_000)

    def test_ground_contact_splits_two_legs(self):
        leg1 = _flight(0, 43.17, -79.93, 49.19, -123.18, minutes=200)
        leg2 = _flight(200 * 60 + 3600, 49.19, -123.18, 43.17, -79.93, minutes=200, callsign="CJT552")
        flights = segment(_points(_doc(leg1 + leg2)), AIRPORTS)
        assert [f["callsign"] for f in flights] == ["CJT551", "CJT552"]

    def test_cruise_coverage_hole_does_not_split(self):
        trace = [p for p in _flight(0, 43.17, -79.93, 49.19, -123.18)
                 if not (100 * 60 <= p[0] <= 160 * 60)]          # 1 h hål på marschhöjd
        assert len(segment(_points(_doc(trace)), AIRPORTS)) == 1

    def test_short_hops_and_taxi_are_dropped(self):
        trace = [[i * 30, 43.17, -79.93, 800, 140, 90, 0, 0, None] for i in range(20)]
        assert segment(_points(_doc(trace)), AIRPORTS) == []

    def test_path_is_downsampled(self):
        doc = _doc(_flight(0, 43.17, -79.93, 49.19, -123.18, minutes=600))
        f = segment(_points(doc), AIRPORTS, max_path_points=50)[0]
        assert len(f["path"]) <= 51


class TestRecentRecorder:
    def test_merge_dedupes_and_prunes(self):
        from atlas_choke.sources.cargojet import merge_recent
        old = [[100.0, 43.0, -79.0, 1000], [200.0, 43.1, -79.1, 2000]]
        live = {"timestamp": 150.0, "trace": [[50.0, 43.1, -79.1, 2000], [100.0, 43.2, -79.2, 3000]]}
        merged = merge_recent(old, live, keep_after=150.0)
        assert [p[0] for p in merged] == [200.0, 250.0]     # 100 rensad, 200 dedupad
        assert merged[1][3] == 3000
