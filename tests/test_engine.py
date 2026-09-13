"""Motor-tester — ren aritmetik, inga nätverksanrop."""

from __future__ import annotations

import datetime

from atlas_choke.engine import events, forecast, stress


def _series(values: list[float], start: str = "2020-01-01") -> list[tuple[str, float]]:
    d0 = datetime.date.fromisoformat(start)
    return [((d0 + datetime.timedelta(days=i)).isoformat(), v)
            for i, v in enumerate(values)]


def _flat_then_drop(n_flat: int = 500, n_drop: int = 60,
                    base: float = 1000.0, drop: float = 200.0):
    # lite brus så MAD inte blir 0
    flat = [base + (i % 7) * 5 for i in range(n_flat)]
    dropped = [drop + (i % 7) * 5 for i in range(n_drop)]
    return _series(flat + dropped)


class TestStress:
    def test_normal_series_scores_neutral(self):
        ser = _series([1000 + (i % 7) * 5 for i in range(600)])
        a = stress.assess(ser)
        assert a is not None
        assert 40 <= a["score"] <= 60
        assert a["level"] == "normalt"
        assert a["ongoing_since"] is None

    def test_drop_detected_as_severe(self):
        a = stress.assess(_flat_then_drop())
        assert a is not None
        assert a["score"] >= 80
        assert a["level"] == "allvarligt"
        assert a["deviation_pct"] < -70

    def test_frozen_baseline_keeps_long_disruption_stressed(self):
        # 300 dagars kollaps — utan frysning hade rullande baslinje ätit upp den
        ser = _flat_then_drop(n_flat=500, n_drop=300)
        a = stress.assess(ser)
        assert a["score"] >= 80
        assert a["ongoing_since"] is not None
        assert a["ongoing_days"] >= 290

    def test_episode_closes_after_recovery(self):
        flat = [1000 + (i % 7) * 5 for i in range(500)]
        dip = [200 + (i % 7) * 5 for i in range(30)]
        rec = [1000 + (i % 7) * 5 for i in range(60)]
        w = stress.walk(_series(flat + dip + rec))
        assert len(w["episodes"]) == 1
        ep = w["episodes"][0]
        assert not ep["ongoing"]
        assert ep["end"] is not None
        assert w["frozen"] is None
        # och nuläget är normalt igen
        a = stress.assess(_series(flat + dip + rec), walked=w)
        assert a["level"] == "normalt"

    def test_inverse_node_stressed_by_rise(self):
        flat = [1000 + (i % 7) * 5 for i in range(500)]
        surge = [1900 + (i % 7) * 5 for i in range(60)]
        a = stress.assess(_series(flat + surge), inverse=True)
        assert a["score"] >= 80
        # samma serie som vanlig nod: en ÖKNING är inte stress
        a2 = stress.assess(_series(flat + surge), inverse=False)
        assert a2["score"] <= 50

    def test_too_short_series_returns_none(self):
        assert stress.assess(_series([1000.0] * 50)) is None


class TestEvents:
    def _hist(self, closes: list[float], start: str = "2020-01-01") -> list[dict]:
        d0 = datetime.date.fromisoformat(start)
        return [{"date": (d0 + datetime.timedelta(days=i)).isoformat(), "close": c}
                for i, c in enumerate(closes)]

    def test_forward_returns_from_episode_start(self):
        hist = self._hist([100.0] * 10 + [110.0] * 30)
        fr = events.forward_returns(hist, "2020-01-05")
        # bas = index 4 (100); +5 hd = index 9 (sista 100:an) → 0 %; +10 hd = 110 → +10 %
        assert fr[5] == 0.0
        assert fr[10] == 10.0

    def test_event_study_pools_across_episodes(self):
        hist = self._hist([100.0] * 20 + [120.0] * 20 + [120.0] * 20 + [150.0] * 40)
        eps = [{"start": "2020-01-10", "end": "2020-01-15", "days": 5,
                "peak_z": 2.0, "peak_date": "2020-01-12", "ongoing": False},
               {"start": "2020-02-25", "end": "2020-03-05", "days": 8,
                "peak_z": 3.0, "peak_date": "2020-03-01", "ongoing": False}]
        study = events.event_study(eps, {"X": hist})
        pooled = study["pooled"]["X"]
        assert pooled[20]["n"] == 2
        assert pooled[5]["median"] > 0

    def test_unconditional_stats_reasonable(self):
        hist = self._hist([100.0 + (i % 11) for i in range(400)])
        st = events.unconditional_stats(hist)
        assert st[5]["n"] > 300
        assert st[5]["p10"] <= st[5]["median"] <= st[5]["p90"]


class TestForecast:
    def test_active_when_ongoing_episode(self):
        assessment = {"directed_z": 0.9, "ongoing_since": "2026-03-04"}
        fc = forecast.outlook(assessment, {"episodes": [], "pooled": {}}, {}, [])
        assert fc["active"] is True
        assert fc["mode"] == "analog"

    def test_inactive_when_calm(self):
        assessment = {"directed_z": 0.3, "ongoing_since": None}
        fc = forecast.outlook(assessment, {"episodes": [], "pooled": {}}, {}, [])
        assert fc["active"] is False
        assert fc["mode"] == "basnivå"

    def test_edge_is_analog_minus_base(self):
        study = {"episodes": [{}, {}, {}],
                 "pooled": {"X": {5: {"n": 3, "median": 4.0, "p10": -1.0, "p90": 9.0}}}}
        base = {"X": {5: {"n": 400, "median": 0.5, "p10": -3.0, "p90": 4.0}}}
        fc = forecast.outlook({"directed_z": 2.0, "ongoing_since": None}, study, base,
                              [{"symbol": "X", "name": "Test", "why": ""}])
        row = fc["instruments"][0]
        assert row["confidence"] == "ok"
        assert row["horizons"][5]["edge_median"] == 3.5


class TestDelays:
    def _cp(self):
        return {"id": "suez", "lat": 30.4, "lon": 32.35}

    def test_calm_conditions_low_risk(self):
        from atlas_choke.engine import delays
        r = delays.assess_chokepoint(self._cp(), {}, [], None, None, None)
        assert r["level"] == "låg"
        assert r["score"] == 0
        assert r["delay_days"] is None

    def test_hard_gusts_raise_risk_and_delay(self):
        from atlas_choke.engine import delays
        r = delays.assess_chokepoint(self._cp(), {"gust_max48_ms": 24.0}, [],
                                     None, None, None)
        assert r["level"] == "förhöjd"
        assert r["delay_days"] == [1, 3]
        assert any("Byar" in x["txt"] for x in r["reasons"])

    def test_red_cyclone_nearby_scores_high(self):
        from atlas_choke.engine import delays
        alert = {"event_code": "TC", "alertlevel": "RED", "kind": "Tropisk cyklon",
                 "name": "TEST-01", "lat": 31.0, "lon": 33.0}
        r = delays.assess_chokepoint(self._cp(), {"gust_max48_ms": 22.0},
                                     [alert], None, None, None)
        assert r["level"] == "hög"

    def test_far_cyclone_ignored(self):
        from atlas_choke.engine import delays
        alert = {"event_code": "TC", "alertlevel": "RED", "kind": "Tropisk cyklon",
                 "name": "FJÄRRAN", "lat": -20.0, "lon": 120.0}
        r = delays.assess_chokepoint(self._cp(), {}, [alert], None, None, None)
        assert r["score"] == 0

    def test_ongoing_episode_uses_alternative_delay(self):
        from atlas_choke.engine import delays
        r = delays.assess_chokepoint(
            self._cp(), {}, [], None,
            {"ongoing_since": "2023-12-27"},
            {"route": "Runt Godahoppsudden", "delay_days": [10, 14]})
        assert r["delay_days"] == [10, 14]
        assert any("Omväg" in x["txt"] for x in r["reasons"])

    def test_lane_risk_is_worst_via(self):
        from atlas_choke.engine import delays
        lanes = [{"id": "l1", "name": "Test", "teu_share_pct": 10,
                  "waypoints": [[0, 0], [1, 1]], "via": ["a", "b"]}]
        per_cp = {"a": {"score": 10, "level": "låg", "delay_days": None},
                  "b": {"score": 70, "level": "hög", "delay_days": [2, 5]}}
        out = delays.assess_lanes(lanes, per_cp)
        assert out[0]["risk_level"] == "hög"
        assert out[0]["worst_via"] == "b"
        assert out[0]["delay_days"] == [2, 5]


class TestDrift:
    CPS = [{"id": "hormuz", "lat": 26.57, "lon": 56.25, "instruments":
            [{"symbol": "BZ=F", "name": "Brent", "why": ""}]}]

    def _ship(self, **kw):
        base = {"mmsi": 1, "name": "TEST", "lat": 26.6, "lon": 56.3,
                "sog": 0.0, "nav": 0, "category": "tanker"}
        base.update(kw)
        return base

    def test_not_under_command_detected_anywhere(self):
        from atlas_choke.engine import drift
        # nav 2 långt från alla sund ska ändå flaggas (hård signal)
        out = drift.own_drifters([self._ship(nav=2, lat=0.0, lon=-30.0)], self.CPS)
        assert len(out) == 1
        assert out[0]["label"] == "ej under kontroll"
        assert out[0]["chokepoint"] is None

    def test_slow_underway_only_inside_zone(self):
        from atlas_choke.engine import drift
        inside = self._ship(nav=0, sog=0.8)
        outside = self._ship(nav=0, sog=0.8, lat=0.0, lon=-30.0)
        out = drift.own_drifters([inside, outside], self.CPS)
        assert len(out) == 1
        assert out[0]["label"] == "misstänkt loitering"
        assert out[0]["chokepoint"] == "hormuz"

    def test_normal_and_anchored_ships_ignored(self):
        from atlas_choke.engine import drift
        normal = self._ship(nav=0, sog=12.0)
        anchored = self._ship(nav=1, sog=0.0)
        assert drift.own_drifters([normal, anchored], self.CPS) == []

    def test_market_note_for_drifting_tanker_near_oil_chokepoint(self):
        from atlas_choke.engine import drift
        d = drift.assemble([self._ship(nav=2)], [], [], [], self.CPS)
        entry = d["per_chokepoint"]["hormuz"]
        assert entry["own_tankers"] == 1
        assert any("Brent-positiv" in n for n in entry["market_notes"])
        assert entry["instruments"] == ["BZ=F"]

    def test_floating_storage_note_for_loitering_tankers(self):
        from atlas_choke.engine import drift
        loit = [{"lat": 26.6, "lon": 56.3, "vessel_type": "tanker",
                 "kind": "loitering"} for _ in range(4)]
        d = drift.assemble([], loit, [], [], self.CPS)
        notes = d["per_chokepoint"]["hormuz"]["market_notes"]
        assert any("flytande lager" in n for n in notes)


class TestNgaParsing:
    def test_first_position_parses_degrees_minutes(self):
        from atlas_choke.sources.nga import _first_position
        pos = _first_position("VESSEL ADRIFT IN 12-34.5N 045-12.3E VICINITY.")
        assert pos is not None
        lat, lon = pos
        assert abs(lat - (12 + 34.5 / 60)) < 0.001
        assert abs(lon - (45 + 12.3 / 60)) < 0.001

    def test_southern_western_hemispheres(self):
        from atlas_choke.sources.nga import _first_position
        lat, lon = _first_position("DERELICT 34-21.6S 018-28.2W REPORTED")
        assert lat < 0 and lon < 0

    def test_no_position_returns_none(self):
        from atlas_choke.sources.nga import _first_position
        assert _first_position("VESSEL ADRIFT SOMEWHERE") is None


class TestScenario:
    def _fixture(self):
        cps = [{"id": "suez", "name": "Suez", "lat": 30.4, "lon": 32.35},
               {"id": "cape-good-hope", "name": "Godahoppsudden",
                "lat": -34.4, "lon": 18.5, "inverse": True},
               {"id": "panama", "name": "Panama", "lat": 9.1, "lon": -79.7}]
        lanes = [{"id": "asia-europe", "name": "Asien-Europa", "teu_share_pct": 22,
                  "waypoints": [[0, 0], [1, 1]], "via": ["suez"]},
                 {"id": "panama-asia", "name": "Panama-Asien", "teu_share_pct": 5,
                  "waypoints": [[0, 0], [1, 1]], "via": ["panama"]}]
        alts = {"suez": {"route": "Runt Godahoppsudden", "delay_days": [10, 14]}}
        bundle = {"chokepoints": [
            {"id": "suez", "stress": None, "episodes": [{}, {}, {}],
             "forecast": {"instruments": [{"symbol": "BDRY", "name": "Frakt",
                 "why": "omväg", "confidence": "ok",
                 "horizons": {"10": {"analog": {"n": 3, "median": 4.0,
                                                "p10": -1.0, "p90": 9.0}}}}]}}]}
        return cps, lanes, alts, bundle

    def test_closing_suez_raises_lane_risk(self):
        from atlas_choke.engine import scenario
        cps, lanes, alts, bundle = self._fixture()
        s = scenario.run("suez", cps, lanes, alts, bundle, {}, {})
        assert s["simulated"] is True
        assert s["per_chokepoint"]["suez"]["level"] == "hög"
        assert s["per_chokepoint"]["suez"]["delay_days"] == [10, 14]
        ids = [l["id"] for l in s["affected_lanes"]]
        assert ids == ["asia-europe"]
        assert s["teu_at_risk_pct"] == 22

    def test_suez_closure_loads_cape(self):
        from atlas_choke.engine import scenario
        cps, lanes, alts, bundle = self._fixture()
        s = scenario.run("suez", cps, lanes, alts, bundle, {}, {})
        # omdirigeringsmottagaren ska belastas, inte ligga kvar på noll
        assert s["per_chokepoint"]["cape-good-hope"]["score"] > 0

    def test_unaffected_lane_stays_low(self):
        from atlas_choke.engine import scenario
        cps, lanes, alts, bundle = self._fixture()
        s = scenario.run("suez", cps, lanes, alts, bundle, {}, {})
        panama = next(l for l in s["lanes"] if l["id"] == "panama-asia")
        assert panama["risk_level"] == "låg"

    def test_unknown_chokepoint(self):
        from atlas_choke.engine import scenario
        cps, lanes, alts, bundle = self._fixture()
        assert "error" in scenario.run("xyz", cps, lanes, alts, bundle, {}, {})


class TestBrief:
    def test_headline_and_sections(self):
        from atlas_choke.engine import brief
        items = [{"id": "suez", "name": "Suez", "score": 90.6, "level": "allvarligt",
                  "deviation_pct": -57.1, "active": True},
                 {"id": "dover", "name": "Dover", "score": 40, "level": "normalt",
                  "active": False}]
        delays = {"per_chokepoint": {"suez": {"level": "hög", "score": 55,
                                              "delay_days": [10, 14]}},
                  "lanes": [{"id": "ae", "name": "Asien-Europa", "risk_level": "hög",
                             "worst_via": "suez", "teu_share_pct": 22}]}
        live = {"totals": {"vessels": 26000, "anchored": 7000},
                "per_chokepoint": {"suez": {"anchored": 10, "ships": 21}}}
        quotes = [{"name": "Brent", "price": 96.3, "change_pct": 6.4}]
        sar = {"scenes": [{"aoi": "hormuz", "scene_time": "2026-09-03T02:14", "n": 1}]}
        b = brief.build(items, delays, live, quotes, sar)
        assert "1 av 2" in b["headline"]
        titles = [s["title"] for s in b["sections"]]
        assert "Stressade flaskhalsar" in titles
        assert any("Live just nu" == t for t in titles)

    def test_calm_world_headline(self):
        from atlas_choke.engine import brief
        items = [{"id": "a", "name": "A", "score": 40, "level": "normalt", "active": False}]
        b = brief.build(items, {"per_chokepoint": {}, "lanes": []},
                        {"totals": {}, "per_chokepoint": {}}, [], {})
        assert "normalläge" in b["headline"]


class TestAir:
    def _flights(self, n_cargo, n_other, near="suez"):
        return ([{"flight": f"FDX{i}", "cargo": True, "near": near} for i in range(n_cargo)]
                + [{"flight": f"BAW{i}", "cargo": False, "near": near} for i in range(n_other)])

    def test_substitution_flag_requires_disruption_and_elevation(self):
        from atlas_choke.engine import air
        cps = [{"id": "suez", "lat": 30.4, "lon": 32.35}]
        flights = self._flights(4, 10)          # 29 % frakt = förhöjt
        out = air.by_chokepoint(flights, cps, {"suez": {"ongoing_since": "2023-12-27"}})
        assert out["suez"]["elevated"] is True
        assert out["suez"]["substitution"] is True
        assert "sjö till luft" in out["suez"]["note"]

    def test_no_substitution_without_disruption(self):
        from atlas_choke.engine import air
        cps = [{"id": "suez", "lat": 30.4, "lon": 32.35}]
        out = air.by_chokepoint(self._flights(4, 10), cps, {"suez": None})
        assert out["suez"]["elevated"] is True
        assert out["suez"]["substitution"] is False

    def test_disrupted_but_normal_air_traffic(self):
        from atlas_choke.engine import air
        cps = [{"id": "suez", "lat": 30.4, "lon": 32.35}]
        out = air.by_chokepoint(self._flights(1, 40), cps,
                                {"suez": {"ongoing_since": "2023-12-27"}})
        assert out["suez"]["substitution"] is False
        assert "ingen tydlig substitution" in out["suez"]["note"]

    def test_small_sample_never_elevated(self):
        from atlas_choke.engine import air
        cps = [{"id": "suez", "lat": 30.4, "lon": 32.35}]
        out = air.by_chokepoint(self._flights(2, 1), cps,
                                {"suez": {"ongoing_since": "x"}})
        assert out["suez"]["elevated"] is False   # < MIN_FLIGHTS_FOR_SHARE

    def test_cargo_classification(self):
        from atlas_choke.sources.adsb import _is_cargo
        assert _is_cargo("FDX1234", "B77L") is True      # FedEx
        assert _is_cargo("GTI9876", "B748") is True      # Atlas Air
        assert _is_cargo("CJT510", "B763") is True       # Cargojet
        assert _is_cargo("BAW117", "B77W") is False      # British Airways passagerare
        assert _is_cargo("SAS1909", "A332") is False     # passagerarbolag, frakttyp
        assert _is_cargo("", "MD11") is True             # ren frakttyp utan callsign


class TestFlags:
    def test_mid_lookup(self):
        from atlas_choke.engine import flags
        assert flags.flag_for(265123456)["iso2"] == "SE"
        assert flags.flag_for(351234567)["foc"] is True          # Panama
        assert flags.flag_for(626123456)["shadow_risk"] is True  # Gabon
        assert flags.flag_for(999) is None
        assert flags.flag_for(None) is None

    def test_enrich_adds_flag_block(self):
        from atlas_choke.engine import flags
        items = flags.enrich([{"mmsi": 265123456}, {"mmsi": 1}])
        assert items[0]["flag"]["country"] == "Sverige"
        assert "flag" not in items[1]

    def test_shadow_exposure_flags_oil_chokepoint(self):
        from atlas_choke.engine import flags
        cps = [{"id": "hormuz", "oil_share_pct": 27}]
        items = ([{"mmsi": 626000000 + i, "category": "tanker", "name": f"T{i}"}
                  for i in range(4)]
                 + [{"mmsi": 265000000 + i, "category": "tanker"} for i in range(6)])
        out = flags.shadow_exposure(flags.enrich(items), cps, lambda v: "hormuz")
        e = out["hormuz"]
        assert e["tankers"] == 10 and e["shadow_flagged"] == 4
        assert e["elevated"] is True and e["oil_chokepoint"] is True
        assert "skuggflotteaktivitet" in e["note"]

    def test_small_tanker_sample_skipped(self):
        from atlas_choke.engine import flags
        cps = [{"id": "hormuz", "oil_share_pct": 27}]
        items = [{"mmsi": 626000001, "category": "tanker"}]
        assert flags.shadow_exposure(items, cps, lambda v: "hormuz") == {}

    def test_normal_fleet_not_elevated(self):
        from atlas_choke.engine import flags
        cps = [{"id": "hormuz", "oil_share_pct": 27}]
        items = [{"mmsi": 265000000 + i, "category": "tanker"} for i in range(20)]
        out = flags.shadow_exposure(flags.enrich(items), cps, lambda v: "hormuz")
        assert out["hormuz"]["elevated"] is False


class TestDarkFleet:
    def _seen(self, hours_ago, cp="hormuz", cat="tanker"):
        import time
        return {"111111111": [time.time() - hours_ago * 3600, 26.5, 56.2,
                              cat, "GA", "TEST TANKER", cp]}

    def test_gap_within_window_is_dark(self):
        from atlas_choke.engine import darkfleet
        out = darkfleet.dark_vessels([], self._seen(12))
        assert len(out) == 1 and out[0]["tanker"] is True
        assert 11 < out[0]["gap_hours"] < 13

    def test_too_recent_is_not_dark(self):
        from atlas_choke.engine import darkfleet
        assert darkfleet.dark_vessels([], self._seen(2)) == []

    def test_too_old_is_gone_not_dark(self):
        from atlas_choke.engine import darkfleet
        assert darkfleet.dark_vessels([], self._seen(100)) == []

    def test_outside_zone_ignored_as_coverage_hole(self):
        from atlas_choke.engine import darkfleet
        assert darkfleet.dark_vessels([], self._seen(12, cp="")) == []

    def test_still_transmitting_not_dark(self):
        from atlas_choke.engine import darkfleet
        cur = [{"mmsi": "111111111", "lat": 26.5, "lon": 56.2}]
        assert darkfleet.dark_vessels(cur, self._seen(12)) == []

    def test_sts_requires_duration(self):
        from atlas_choke.engine import darkfleet
        darkfleet._pairs.clear()
        pair = [{"mmsi": "1", "lat": 25.0, "lon": 55.0, "sog": 0.2, "category": "tanker"},
                {"mmsi": "2", "lat": 25.0008, "lon": 55.0, "sog": 0.1, "category": "tanker"}]
        assert darkfleet.sts_candidates(pair) == []          # första observationen
        import time
        for k in darkfleet._pairs:
            darkfleet._pairs[k] = time.time() - 40 * 60      # håll ihop 40 min
        out = darkfleet.sts_candidates(pair)
        assert len(out) == 1 and out[0]["both_tankers"] is True
        assert out[0]["distance_m"] < 500

    def test_sts_ignores_moving_vessels(self):
        from atlas_choke.engine import darkfleet
        darkfleet._pairs.clear()
        pair = [{"mmsi": "1", "lat": 25.0, "lon": 55.0, "sog": 9.0, "category": "tanker"},
                {"mmsi": "2", "lat": 25.0008, "lon": 55.0, "sog": 8.0, "category": "tanker"}]
        assert darkfleet.sts_candidates(pair) == []


class TestMilitary:
    def test_military_near_chokepoint(self):
        from atlas_choke.engine import air
        cps = [{"id": "hormuz", "lat": 26.57, "lon": 56.25}]
        mil = [{"flight": "RCH123", "type": "C17", "lat": 26.6, "lon": 56.3},
               {"flight": "FAR999", "type": "C130", "lat": 10.0, "lon": 0.0}]
        out = air.military_near(mil, cps)
        assert out["hormuz"]["count"] == 1
        assert out["hormuz"]["closest_km"] < 50

    def test_no_military_gives_empty(self):
        from atlas_choke.engine import air
        assert air.military_near([], [{"id": "x", "lat": 0, "lon": 0}]) == {}


class TestWatchlist:
    def test_watchlist_loaded(self):
        from atlas_choke.engine import flags
        wl = flags._watchlist()
        assert len(wl) > 1000
        assert all(len(k) == 9 and k.isdigit() for k in list(wl)[:20])

    def test_enrich_marks_watchlisted_vessel(self):
        from atlas_choke.engine import flags
        mmsi = next(iter(flags._watchlist()))
        out = flags.enrich([{"mmsi": mmsi}, {"mmsi": "265999999"}])
        assert out[0]["watchlist"]["source"].startswith("shadow-fleet")
        assert "watchlist" not in out[1]

    def test_watchlist_hit_dominates_note(self):
        from atlas_choke.engine import flags
        mmsi = next(iter(flags._watchlist()))
        cps = [{"id": "hormuz", "oil_share_pct": 27}]
        items = flags.enrich(
            [{"mmsi": mmsi, "category": "tanker", "name": "WATCHED"}]
            + [{"mmsi": str(265000000 + i), "category": "tanker"} for i in range(6)])
        out = flags.shadow_exposure(items, cps, lambda v: "hormuz")
        assert out["hormuz"]["watchlisted"] == 1
        assert "bevakningslistan" in out["hormuz"]["note"]
