"""Segmentering och blocktid på syntetiska spår — inga nätverksanrop."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cjt_nowcast import flights
from cjt_nowcast.fleet import hex_from_registration

AIRPORTS = pd.DataFrame([
    {"code": "YHM", "icao": "CYHM", "name": "Hamilton", "country": "CA", "continent": "NA",
     "type": "medium_airport", "lat": 43.1736, "lon": -79.935, "elev_ft": 780.0},
    {"code": "YVR", "icao": "CYVR", "name": "Vancouver", "country": "CA", "continent": "NA",
     "type": "large_airport", "lat": 49.1939, "lon": -123.1844, "elev_ft": 14.0},
    {"code": "YWG", "icao": "CYWG", "name": "Winnipeg", "country": "CA", "continent": "NA",
     "type": "large_airport", "lat": 49.91, "lon": -97.2399, "elev_ft": 783.0},
    {"code": "YKF", "icao": "CYKF", "name": "Kitchener", "country": "CA", "continent": "NA",
     "type": "medium_airport", "lat": 43.4608, "lon": -80.3786, "elev_ft": 1055.0},
    {"code": "MIA", "icao": "KMIA", "name": "Miami", "country": "US", "continent": "NA",
     "type": "large_airport", "lat": 25.7959, "lon": -80.287, "elev_ft": 8.0},
])
T0 = 1_780_000_000.0


@pytest.fixture(scope="module")
def lookup():
    return flights.AirportLookup(AIRPORTS)


def _ground(rows, t_from, t_to, ap, gs, step=30):
    lat, lon = AIRPORTS.set_index("code").loc[ap, ["lat", "lon"]]
    for t in np.arange(t_from, t_to, step):
        rows.append((T0 + t, lat + 0.005, lon, "ground", gs, None, "CJT123"))


def _air(rows, t_from, t_to, lat_from, lon_from, lat_to, lon_to, alt_from, alt_to, step=30):
    n = max(int((t_to - t_from) / step), 2)
    for k in range(n):
        f = k / (n - 1)
        rate = (alt_to - alt_from) / max(t_to - t_from, 1) * 60
        rows.append((T0 + t_from + f * (t_to - t_from), lat_from + f * (lat_to - lat_from),
                     lon_from + f * (lon_to - lon_from), alt_from + f * (alt_to - alt_from),
                     250.0, rate, "CJT123"))


def _yhm_yvr_fully_observed():
    """Uppställd 30 min, taxi 12 min, flyger ~4 h, taxi 6 min, uppställd 20 min."""
    rows = []
    _ground(rows, 0, 1800, "YHM", 0.0)                       # uppställd
    _ground(rows, 1800, 1800 + 720, "YHM", 15.0)             # taxi ut 12 min
    t_lift = 1800 + 720
    _air(rows, t_lift + 20, t_lift + 600, 43.18, -79.94, 43.5, -81.0, 1200, 3500)  # låg stigning
    _air(rows, t_lift + 700, t_lift + 13000, 43.6, -82.0, 49.0, -121.5, 20000, 36000, step=120)
    t_land = t_lift + 14400
    _air(rows, t_land - 600, t_land - 20, 49.1, -122.6, 49.19, -123.17, 3500, 50)
    _ground(rows, t_land, t_land + 360, "YVR", 12.0)          # taxi in 6 min
    _ground(rows, t_land + 360, t_land + 360 + 1200, "YVR", 0.0)
    return rows, 1800.0, t_land + 360.0


def test_fully_observed_block_time(lookup):
    rows, off_expected, on_expected = _yhm_yvr_fully_observed()
    segs = flights.segment(flights.points_from_rows(rows), lookup, "c0003e")
    assert len(segs) == 1
    s = segs[0]
    assert (s["dep"], s["arr"]) == ("YHM", "YVR")
    assert s["to_q"] == "obs" and s["ld_q"] == "obs"
    # sista stillastående punkt före taxi (±1 rapportintervall)
    assert abs(s["off_block"] - (T0 + off_expected)) <= 60
    assert abs(s["on_block"] - (T0 + on_expected)) <= 60


def test_missing_ground_coverage_uses_taxi_estimate(lookup):
    rows, *_ = _yhm_yvr_fully_observed()
    # ta bort all markdata vid YHM — som på navet 2024
    yhm_lat = AIRPORTS.set_index("code").at["YHM", "lat"]
    rows = [r for r in rows if not (r[3] == "ground" and abs(r[1] - yhm_lat) < 0.1)]
    raw = pd.DataFrame(flights.segment(flights.points_from_rows(rows), lookup, "c0003e"))
    assert len(raw) == 1 and raw.at[0, "off_block"] is None
    df, info = flights.fill(raw)
    assert df.at[0, "off_q"] == "taxi_est"
    # utan observerade taxitider används global fallback (12 min)
    taxi_out_min = (df.at[0, "takeoff"] - df.at[0, "off_block"]) / 60
    assert taxi_out_min == pytest.approx(12.0, abs=0.01)


def test_unseen_leg_is_inferred_from_airport_change(lookup):
    rows = []
    _ground(rows, 0, 3600, "YWG", 0.0)
    # planet dyker nästa gång upp på YVR 5 h senare — ingen luftdata emellan
    _ground(rows, 3600 + 5 * 3600, 3600 + 6 * 3600, "YVR", 0.0)
    raw = pd.DataFrame(flights.segment(flights.points_from_rows(rows), lookup, "c04aee"))
    assert len(raw) == 1
    df, _ = flights.fill(raw)
    assert df.at[0, "quality"] == "inferred"
    assert 2.0 < df.at[0, "block_h"] < 4.5


def test_single_altitude_glitch_does_not_split_visit(lookup):
    rows = []
    _ground(rows, 0, 3600, "YHM", 0.0)
    lat, lon = AIRPORTS.set_index("code").loc["YHM", ["lat", "lon"]]
    rows.append((T0 + 3610, lat, lon, 31000, 0.0, None, ""))   # en felaktig höjdrapport
    _ground(rows, 3620, 7200, "YHM", 0.0)
    assert flights.segment(flights.points_from_rows(rows), lookup, "x") == []


def test_small_field_next_to_major_airport_is_shadowed():
    """Markpunkter på YVR får aldrig matchas mot ett icke-IATA-fält 2 km bort."""
    extra = pd.DataFrame([{"code": "CA-9999", "icao": "CA-9999", "name": "Heliport vid YVR",
                           "country": "CA", "continent": "NA", "type": "medium_airport",
                           "lat": 49.2079, "lon": -123.1844, "elev_ft": 10.0}])
    lk = flights.AirportLookup(pd.concat([AIRPORTS, extra], ignore_index=True))
    assert "CA-9999" not in set(lk.df["code"])
    idx, _ = lk.nearest(np.array([49.207]), np.array([-123.1844]))
    assert lk.df.at[idx[0], "code"] == "YVR"


def test_low_overflight_of_nearby_field_is_not_a_visit(lookup):
    """Inflygning YWG→YHM passerar YKF på ~2 500 ft över fältet — får inte bli ett ben."""
    rows = []
    _ground(rows, 0, 1800, "YWG", 0.0)
    _air(rows, 1860, 2400, 49.92, -97.3, 49.5, -96.0, 1200, 3500)
    _air(rows, 2500, 9000, 49.4, -95.0, 43.6, -80.6, 30000, 12000, step=120)
    _air(rows, 9060, 9600, 43.47, -80.40, 43.2, -80.0, 3600, 3400)      # över YKF, 2 500 ft AGL
    _air(rows, 9660, 10200, 43.19, -79.97, 43.175, -79.94, 2500, 800)
    _ground(rows, 10260, 10800, "YHM", 0.0)
    segs = flights.segment(flights.points_from_rows(rows), lookup, "x")
    assert [(s["dep"], s["arr"]) for s in segs] == [("YWG", "YHM")]


def test_same_airport_far_rotation_becomes_roundtrip(lookup):
    """MIA → (osedd mellanlandning ~1 500 km bort) → MIA: två cykler, vändtid dras av."""
    rows = []
    _ground(rows, 0, 1800, "MIA", 0.0)
    _ground(rows, 1800, 2400, "MIA", 15.0)
    _air(rows, 2430, 3000, 25.8, -80.3, 25.5, -80.8, 800, 3900)
    _air(rows, 3100, 6000, 25.0, -81.5, 18.0, -75.0, 20000, 36000, step=120)   # utåt, sedan tappad
    _air(rows, 6000 + 5 * 3600, 6000 + 8 * 3600, 17.0, -74.0, 25.0, -80.0, 36000, 20000, step=120)
    t_land = 6000 + 8 * 3600 + 600
    _air(rows, t_land - 540, t_land - 30, 25.6, -80.1, 25.79, -80.28, 3000, 60)
    _ground(rows, t_land, t_land + 1800, "MIA", 0.0)
    raw = pd.DataFrame(flights.segment(flights.points_from_rows(rows), lookup, "c0816e"))
    assert len(raw) == 1
    df, info = flights.fill(raw)
    r = df.iloc[0]
    assert r["quality"] == "roundtrip_inferred" and r["cycles"] == 2
    span_h = (r["on_block"] - r["off_block"]) / 3600
    one_leg_h = (info["airborne_a_min"] + info["airborne_b_min_per_km"] * r["max_km_from_dep"]) / 60
    # blocktid = min(total − vändtid, tur och retur till längsta punkten) — aldrig hela uppehållet ute
    assert r["block_h"] <= span_h - info["turnaround_h"] + 1e-6
    assert r["block_h"] <= 2 * one_leg_h + 1.0


def test_route_rules_latam_and_specificity():
    from cjt_nowcast import routes
    rules = pd.DataFrame([
        {"dep": "MIA", "arr": "MIA", "segment": "acmi", "directional": "", "note": ""},
        {"dep": "CVG", "arr": "latam", "segment": "acmi", "directional": "", "note": ""},
        {"dep": "cc:CA", "arr": "cc:CA", "segment": "domestic", "directional": "", "note": ""},
        {"dep": "*", "arr": "*", "segment": "charter", "directional": "", "note": ""},
    ])
    rules["_spec"] = rules["dep"].map(routes._specificity) + rules["arr"].map(routes._specificity)
    rules = rules.sort_values("_spec", ascending=False, kind="stable")

    def seg(dep, dc, dk, arr, ac, ak):
        r = {"dep": dep, "dep_country": dc, "dep_continent": dk, "arr": arr, "arr_country": ac, "arr_continent": ak}
        return routes.classify(r, rules)
    assert seg("MTY", "MX", "NA", "CVG", "US", "NA") == "acmi"          # söderut, båda riktningar
    assert seg("CVG", "US", "NA", "YVR", "CA", "NA") == "charter"       # transborder = charter
    assert seg("YHM", "CA", "NA", "YVR", "CA", "NA") == "domestic"
    assert seg("MIA", "US", "NA", "MIA", "US", "NA") == "acmi"


@pytest.mark.parametrize("reg,hexid", [("C-FACJ", "c0003e"), ("C-FCJF", "c00638"),
                                        ("C-GCJT", "c04aee"), ("C-FAAA", "c00001")])
def test_canadian_hex_from_registration(reg, hexid):
    assert hex_from_registration(reg) == hexid
