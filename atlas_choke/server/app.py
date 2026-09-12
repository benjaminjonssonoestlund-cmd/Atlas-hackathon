"""Atlas Chokepoint — API-server (FastAPI) + statisk 3D-frontend.

Fokuserad efterföljare till atlas-earth: EN funktion, byggd på riktig data.
Alla tunga externa anrop cachas (samma cache-motor som atlas-earth); endpoints
returnerar alltid 200 med tomma strukturer vid källfel — frontenden degraderar,
den dör inte.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..cache import CACHE
from ..config import CONFIG
from ..engine import air as air_engine
from ..engine import brief as brief_engine
from ..engine import darkfleet as dark_engine
from ..engine import delays as delays_engine
from ..engine import scenario as scenario_engine
from ..engine import drift as drift_engine
from ..engine import events as events_engine
from ..engine import flags as flags_engine
from ..engine import forecast as forecast_engine
from ..engine import live as live_engine
from ..engine import orbits as orbits_engine
from ..engine import ports as ports_engine
from ..engine import stress as stress_engine
from ..sources.adsb import AdsbSource
from ..sources.aiscatcher_local import AisCatcherLocalSource
from ..sources.aisstream import AisStreamSource
from ..sources.barentswatch import BarentsWatchSource
from ..sources.digitraffic import DigitrafficSource
from ..sources.gdacs import GdacsSource
from ..sources.gfw import GfwSource
from ..sources.markets import MarketSource
from ..sources.nga import NgaWarningsSource
from ..sources.opensky import OpenSkySource
from ..sources.portwatch import PortWatchSource
from ..sources.satellites import SatelliteSource
from ..sources.weather import ChokepointWeatherSource

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("atlas.choke")

STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

app = FastAPI(title="Atlas Chokepoint", version="0.1.0")

portwatch = PortWatchSource()
markets = MarketSource()
ais = AisStreamSource()
digitraffic = DigitrafficSource()
barentswatch = BarentsWatchSource()
aiscatcher = AisCatcherLocalSource()
adsb = AdsbSource()
weather = ChokepointWeatherSource()
gdacs = GdacsSource()
gfw = GfwSource()
nga = NgaWarningsSource()
opensky = OpenSkySource()
satellites = SatelliteSource()

CHOKEPOINTS: list[dict] = json.loads(
    (DATA_DIR / "chokepoints.json").read_text(encoding="utf-8"))["chokepoints"]
_LANES_DATA = json.loads((DATA_DIR / "lanes.json").read_text(encoding="utf-8"))
LANES: list[dict] = _LANES_DATA["lanes"]
ALTERNATIVES: dict = _LANES_DATA["chokepoint_alternatives"]
PW_NAMES = tuple(cp["portwatch"] for cp in CHOKEPOINTS)
ALL_SYMBOLS = sorted({i["symbol"] for cp in CHOKEPOINTS for i in cp["instruments"]})

BUNDLE_TTL = 21600  # 6 h — underliggande hämtningar har egna TTL:er
# Versionen MÅSTE ingå i cachenyckeln: annars serverar cachen en bundle byggd
# med gamla motorparametrar i upp till 6 h efter en kodändring (klassisk fälla
# från atlas-earth: "ändringen ser ut att sakna effekt").
BUNDLE_KEY = f"engine:bundle:v3:{CONFIG.portwatch_since}"


def _build_bundle() -> dict:
    """Hela analysen i ett svep: serier → stress → episoder → prognos.

    Körs sällan (cachas) och i trådpool/bakgrund — aldrig i requestvägen.
    """
    t0 = time.time()
    rows = portwatch.safe_fetch_series(PW_NAMES)
    facts = portwatch.safe_facts_by_name()
    series = stress_engine.series_by_name(rows, metric="capacity")
    series_n = stress_engine.series_by_name(rows, metric="n_total")

    # Prishistorik hämtas EN gång per symbol (BZ=F delas av sex sund).
    histories = {s: markets.safe_fetch_history(s, CONFIG.portwatch_since)
                 for s in ALL_SYMBOLS}
    base_stats = {s: events_engine.unconditional_stats(h)
                  for s, h in histories.items() if h}

    out = []
    for cp in CHOKEPOINTS:
        name = cp["portwatch"]
        ser = series.get(name, [])
        inverse = bool(cp.get("inverse"))
        walked = stress_engine.walk(ser, inverse) if ser else {"daily": [], "episodes": [], "frozen": None}
        assessment = stress_engine.assess(ser, inverse, walked) if ser else None
        episodes = walked["episodes"]
        cp_hist = {i["symbol"]: histories.get(i["symbol"], [])
                   for i in cp["instruments"]}
        study = events_engine.event_study(episodes, {k: v for k, v in cp_hist.items() if v})
        fc = forecast_engine.outlook(assessment, study, base_stats, cp["instruments"])
        # n_total-sparkline som komplement (antal skrov, lättare att intuitivt läsa)
        ser_n = series_n.get(name, [])
        out.append({
            **{k: cp[k] for k in ("id", "name", "lat", "lon", "oil_share_pct",
                                  "trade_note", "watch")},
            "inverse": inverse,
            "portwatch_name": name,
            "facts": facts.get(name, {}),
            "stress": assessment,
            "episodes": study["episodes"],
            "forecast": fc,
            "transits_spark": [{"date": d, "value": round(v)} for d, v in ser_n[-120:]],
            # veckosamplad riktad z sedan 2019 — tidsmaskinens råvara
            "z_weekly": [[d, round(z, 2)] for d, z in walked["daily"][::7]],
        })
    bundle = {"generated": time.time(), "build_s": round(time.time() - t0, 1),
              "since": CONFIG.portwatch_since, "chokepoints": out}
    log.info("analys-bundle byggd på %.1f s (%d sund, %d symboler)",
             bundle["build_s"], len(out), len(ALL_SYMBOLS))
    return bundle


def _bundle() -> dict:
    return CACHE.get_or_fetch(BUNDLE_KEY, BUNDLE_TTL, _build_bundle) \
        or {"generated": 0, "chokepoints": []}


_build_lock = threading.Lock()
_build_thread: threading.Thread | None = None


def _bundle_nowait() -> dict | None:
    """Bundeln om den finns i cachen, annars None + bygge i bakgrunden.

    Kallstartsbygget tar ~30-60 s (7 års satellit-AIS + prishistorik) — det
    får ALDRIG blockera ett HTTP-svar; frontenden pollar i stället."""
    global _build_thread
    b = CACHE.get(BUNDLE_KEY)
    if b is not None:
        return b
    with _build_lock:
        if _build_thread is None or not _build_thread.is_alive():
            _build_thread = threading.Thread(target=_bundle, daemon=True)
            _build_thread.start()
    return None


def _grid_loop() -> None:
    """Globalt ADS-B-svep i bakgrunden. ~50 s per varv (artig takthållning)
    — får ALDRIG ligga i requestvägen; endpointen serverar bara cachen."""
    while True:
        try:
            n = len(adsb.safe_fetch_grid())
            log.info("adsb-rutnät uppdaterat: %d flygplan", n)
        except Exception as exc:  # noqa: BLE001 — svepet får aldrig döda tråden
            log.warning("adsb-rutnät: %s", exc)
        time.sleep(540)


@app.on_event("startup")
def _warm() -> None:
    # Bygg bundeln i bakgrunden direkt vid start så första klicket är varmt.
    _bundle_nowait()
    ais.ensure_started()
    threading.Thread(target=_grid_loop, daemon=True).start()


# ---------- API ----------

@app.get("/api/config")
def api_config() -> dict:
    return {
        "cesium_ion_token": CONFIG.cesium_ion_token,
        "ais_available": ais.available,
        "since": CONFIG.portwatch_since,
    }


@app.get("/api/overview")
def api_overview() -> dict:
    """Globlagret: alla sund med stressläge — lätt nog att polla."""
    b = _bundle_nowait()
    if b is None:
        return {"building": True, "generated": 0, "items": []}
    items = []
    for cp in b.get("chokepoints", []):
        s = cp.get("stress") or {}
        items.append({
            "id": cp["id"], "name": cp["name"],
            "lat": cp["lat"], "lon": cp["lon"],
            "inverse": cp["inverse"],
            "score": s.get("score"), "level": s.get("level"),
            "directed_z": s.get("directed_z"),
            "deviation_pct": s.get("deviation_pct"),
            "last_date": s.get("last_date"),
            "n_episodes": len(cp.get("episodes", [])),
            "active": bool((cp.get("forecast") or {}).get("active")),
        })
    return {"generated": b.get("generated", 0), "since": b.get("since", ""),
            "items": items}


@app.get("/api/chokepoint/{cp_id}")
def api_chokepoint(cp_id: str) -> dict:
    b = _bundle()
    for cp in b.get("chokepoints", []):
        if cp["id"] == cp_id:
            return cp
    return {"error": "okänt sund", "id": cp_id}


@app.get("/api/markets")
def api_markets() -> dict:
    """Live-citat för alla kopplade instrument (tickern + panelerna)."""
    quotes = markets.safe_fetch_quotes(ALL_SYMBOLS)
    named = {}
    for cp in CHOKEPOINTS:
        for inst in cp["instruments"]:
            named.setdefault(inst["symbol"], inst["name"])
    return {"quotes": [{"symbol": s, "name": named.get(s, s), **q}
                       for s, q in quotes.items()]}


_merged_memo: dict = {"ts": 0.0, "items": []}
_merged_lock = threading.Lock()


def _merged_vessels() -> list[dict]:
    """AISStream-snapshot + Digitraffic + BarentsWatch, mergat per MMSI.

    Mergemönstret är atlas-earths: nationella öppna feeds fyller ut där
    frivilligmottagarna är glesa. Memoiseras I MINNET (inte sqlite-cachen):
    vid global prenumeration är listan 100k+ poster ≈ tiotals MB — att pickla
    den till disk var 45:e sekund hade slitit mer än den sparat."""
    with _merged_lock:
        if time.time() - _merged_memo["ts"] < 45 and _merged_memo["items"]:
            return _merged_memo["items"]
        by_mmsi: dict = {}
        # lägsta prioritet först — senare källor skriver över per MMSI
        for item in barentswatch.safe_fetch_items():
            by_mmsi[item["mmsi"]] = item
        for item in digitraffic.safe_fetch_items():
            by_mmsi[item["mmsi"]] = item
        # egen AIS-catcher-station (färskast lokalt — hög prioritet)
        for item in aiscatcher.safe_fetch_items():
            by_mmsi[item["mmsi"]] = item
        for item in ais.snapshot() or []:
            item.setdefault("src", "aisstream")
            by_mmsi[item["mmsi"]] = item
        items = flags_engine.enrich(list(by_mmsi.values()))
        if items:
            _merged_memo.update(ts=time.time(), items=items)
        return items


@app.get("/api/vessels")
def api_vessels() -> Response:
    """Mergad live-fartygsbild: AISStream (korridorer) + Digitraffic (Östersjön)
    + BarentsWatch (norska EEZ, valfritt konto)."""
    items = _merged_vessels()
    counts: dict[str, int] = {}
    for i in items:
        counts[i.get("src", "aisstream")] = counts.get(i.get("src", "aisstream"), 0) + 1
    return Response(content=json.dumps({
        "items": items, "count": len(items), "sources": counts,
        "available": ais.available}), media_type="application/json")


_transits = live_engine.TransitCounter()


@app.get("/api/live")
def api_live() -> dict:
    """Nulägesbilden per sund: fartyg i zonen, ankrade köer, passagetakt —
    plus norm ur PortWatch (historikens roll: baslinje som nuet jämförs mot)."""
    def _compute():
        items = _merged_vessels()
        stats = live_engine.live_stats(items, CHOKEPOINTS)
        entries = _transits.update(items, CHOKEPOINTS)
        live_engine.record(stats)
        # norm ur PortWatch-fakta: passager/dygn (7-årsgenomsnitt)
        facts = portwatch.safe_facts_by_name()
        hours_running = max((time.time() - _transits.started) / 3600, 1e-9)
        for cp in CHOKEPOINTS:
            f = facts.get(cp["portwatch"], {})
            vpy = f.get("vessels_per_year")
            entry = stats["per_chokepoint"].get(cp["id"])
            if entry is not None:
                entry["norm_transits_per_day"] = round(vpy / 365) if vpy else None
                entry["trend_24h"] = live_engine.trend(cp["id"])
                entry["entries_since_start"] = entries.get(cp["id"], 0)
        stats["counter_started"] = _transits.started
        stats["counter_hours"] = round(hours_running, 1)
        return stats

    return CACHE.get_or_fetch("live:stats", 60, _compute) or \
        {"per_chokepoint": {}, "totals": {}, "ts": 0}


@app.get("/api/delays")
def api_delays() -> dict:
    """Förseningsrisk per sund + handelsväg: väder (48 h prognos) + GDACS-larm
    + live-kö + pågående stressepisod, med läsbara skäl per poängbidrag."""
    def _compute():
        wx = weather.safe_fetch(CHOKEPOINTS)
        alerts = gdacs.safe_fetch_alerts()
        disruptions = portwatch.safe_fetch_disruptions(30)
        live = live_engine.live_stats(_merged_vessels(), CHOKEPOINTS)["per_chokepoint"]
        bundle = _bundle_nowait() or {"chokepoints": []}
        stress_by_id = {c["id"]: c.get("stress") for c in bundle.get("chokepoints", [])}
        per_cp = {}
        for cp in CHOKEPOINTS:
            per_cp[cp["id"]] = delays_engine.assess_chokepoint(
                cp, wx.get(cp["id"], {}), alerts,
                live.get(cp["id"]), stress_by_id.get(cp["id"]),
                ALTERNATIVES.get(cp["id"]), disruptions)
        lanes = delays_engine.assess_lanes(LANES, per_cp)
        # relevanta larm till kartan (RÖD/ORANGE cykloner/tsunamier)
        map_alerts = [a for a in alerts
                      if a.get("event_code") in ("TC", "TS")
                      and a.get("alertlevel") in ("RED", "ORANGE")]
        return {"per_chokepoint": per_cp, "lanes": lanes,
                "alerts": map_alerts[:40], "ts": time.time()}

    return CACHE.get_or_fetch("delays:all", CONFIG.ttl_medium, _compute) or \
        {"per_chokepoint": {}, "lanes": [], "alerts": []}


@app.get("/api/ports")
def api_ports() -> dict:
    """Världens ~400 största hamnar (PortWatch) + live-inbound ur AIS-dest.

    Historiken (årsvolym, andel av landets sjöhandel) kommer ur PortWatch;
    live-blocket räknar fartyg som JUST NU deklarerat hamnen som destination
    — ett golv, inte en totalräkning (bara ~1/3 av fartygen bär dest-fält)."""
    def _compute():
        layer = portwatch.safe_fetch_ports_layer(400)
        inbound = ports_engine.inbound_by_alias(_merged_vessels())
        ports_engine.attach_inbound(layer, inbound)
        return {"items": layer,
                "disruptions": portwatch.safe_fetch_disruptions(180)[:60],
                "ts": time.time()}

    return CACHE.get_or_fetch("ports:layer", 120, _compute) or \
        {"items": [], "disruptions": []}


@app.get("/api/port/{pw_id}/daily")
def api_port_daily(pw_id: str) -> dict:
    """Dagliga satellit-AIS-anlöp + import/export-ton för en hamn (120 d)."""
    if not pw_id.replace("_", "").isalnum():
        return {"error": "ogiltigt portid", "series": []}
    return {"series": portwatch.safe_fetch_port_daily(pw_id, 120)}


@app.get("/api/drift")
def api_drift() -> dict:
    """Drivande/loitrande fartyg (egen AIS + GFW + NGA) → råvarukoppling.

    Egen AIS: NavStatus 2/3 = fartygets egen haveri-deklaration; zon-loitering.
    GFW (token): loitering-/omlastningsevents ur satellit-AIS, 7 dagar.
    NGA (nyckelfritt): officiella "VESSEL ADRIFT"-varningar med position."""
    def _compute():
        items = _merged_vessels()
        return {
            **drift_engine.assemble(
                items,
                gfw.safe_fetch_events("loitering", 7),
                gfw.safe_fetch_events("encounters", 7),
                nga.safe_fetch_drift_warnings(),
                CHOKEPOINTS),
            "gfw_available": gfw.available,
            "ts": time.time(),
        }

    return CACHE.get_or_fetch("drift:all", 300, _compute) or \
        {"own": [], "gfw_loitering": [], "gfw_encounters": [],
         "nga_warnings": [], "per_chokepoint": {}, "totals": {},
         "gfw_available": gfw.available}


@app.get("/api/darkfleet")
def api_darkfleet() -> dict:
    """Mörka fartyg (AIS-gap 6-72 h) + omlastningar till havs (STS).

    Trösklar från thanderoy/ais-tracker. Att stänga av AIS är den klassiska
    metoden att dölja sanktionerad last; ett mörkt tankfartyg vid ett oljesund
    är den starkaste sanktionsindikator öppna data kan ge."""
    def _compute():
        items = _merged_vessels()
        seen = dark_engine.update_seen(items, CHOKEPOINTS)
        dark = dark_engine.dark_vessels(items, seen)
        sts = dark_engine.sts_candidates(
            items, portwatch.safe_fetch_ports_layer(400), CHOKEPOINTS)
        return {
            "dark": dark[:60],
            "dark_total": len(dark),
            "dark_tankers": sum(1 for d in dark if d["tanker"]),
            "sts": sts[:40],
            "sts_total": len(sts),
            "tracked_vessels": len(seen),
            "ts": time.time(),
        }

    return CACHE.get_or_fetch("dark:all", 240, _compute) or         {"dark": [], "sts": [], "dark_total": 0, "sts_total": 0}


@app.get("/api/shadowfleet")
def api_shadowfleet() -> dict:
    """Skuggflotte-exponering per sund: andel tankers under högriskflagg.

    Flaggstaten ligger i MMSI:s tre första siffror (ITU MID). Register vars
    tankflottor växte kraftigt efter 2022 pekas återkommande ut i
    sanktionsrapportering — onormal andel vid ett oljesund är en observerbar
    indikator på sanktionsflöden. Flagg är indikation, inte bevis."""
    def _compute():
        items = _merged_vessels()

        def zone_of(v):
            for cp in CHOKEPOINTS:
                radius = (live_engine.CAPE_RADIUS_KM
                          if cp["id"] == "cape-good-hope" else live_engine.ZONE_RADIUS_KM)
                if abs(v["lat"] - cp["lat"]) > 2.5 or abs(v["lon"] - cp["lon"]) > 3.5:
                    continue
                if live_engine.haversine_km(cp["lat"], cp["lon"],
                                            v["lat"], v["lon"]) <= radius:
                    return cp["id"]
            return None

        watch = [{"mmsi": v["mmsi"],
                  "name": v.get("name") or v["watchlist"].get("listed_name") or "",
                  "imo": v["watchlist"].get("imo", ""),
                  "category": v.get("category"), "lat": v["lat"], "lon": v["lon"],
                  "flag": (v.get("flag") or {}).get("country", ""),
                  "sog": v.get("sog"), "dest": v.get("dest", "")}
                 for v in items if v.get("watchlist")]
        return {"per_chokepoint": flags_engine.shadow_exposure(
                    items, CHOKEPOINTS, zone_of),
                "watchlist_live": watch,
                "watchlist_total": len(watch),
                "watchlist_size": len(flags_engine._watchlist()),
                "attribution": "Skuggflottelista: FormerLab/shadow-fleet-tracker-light (MIT)",
                "ts": time.time()}

    return CACHE.get_or_fetch("flags:shadow", 180, _compute) or {"per_chokepoint": {}}


@app.get("/api/flights")
def api_flights() -> dict:
    """Live flygtrafik runt flaskhalsarna (ADS-B, keyless) + fraktandel.

    Flygfrakt är substitutionskanalen när sjöfrakt stryps — hög fraktandel
    över ett stört sund bekräftar att störningen biter i realekonomin."""
    def _compute():
        # Zonfrågorna (ADS-B-nätverken) ger typkod och tät täckning nära
        # sunden; OpenSky ger hela världen. Mergen dedupas på hex och
        # zon-träffarna vinner eftersom de bär `near` + aircraft type.
        zone = adsb.safe_fetch_around(CHOKEPOINTS)
        by_hex = {f["hex"]: f for f in opensky.safe_fetch_global()}
        grid = CACHE.get("adsbgrid:250") or []      # bakgrundssvepets resultat
        n_grid_new = 0
        for f in grid:
            if f["hex"] not in by_hex:
                n_grid_new += 1
            by_hex[f["hex"]] = f
        for f in zone:
            by_hex[f["hex"]] = f
        flights = list(by_hex.values())
        bundle = _bundle_nowait() or {"chokepoints": []}
        stress_by_id = {c["id"]: c.get("stress") for c in bundle.get("chokepoints", [])}
        mil = adsb.safe_fetch_military()
        return {"items": flights, "military": mil,
                "per_chokepoint": air_engine.by_chokepoint(
                    zone, CHOKEPOINTS, stress_by_id),
                "military_near": air_engine.military_near(mil, CHOKEPOINTS),
                "totals": air_engine.totals(flights),
                "sources": {"zon (ADS-B-nätverk)": len(zone),
                            "rutnät (ADS-B, unika)": n_grid_new,
                            "globalt (OpenSky)": len(flights) - len(zone) - n_grid_new},
                "attribution": "ADS-B: adsb.lol (ODbL 1.0) / adsb.one",
                "ts": time.time()}

    return CACHE.get_or_fetch("adsb:layer", 90, _compute) or \
        {"items": [], "per_chokepoint": {}, "totals": {}}


@app.get("/api/scenario/{cp_id}")
def api_scenario(cp_id: str) -> dict:
    """Kontrafaktiskt: vad händer om detta sund stängs? Samma motorer som
    live-analysen, med sundet tvingat till full stress. Allt märks simulated."""
    bundle = _bundle_nowait() or {"chokepoints": []}
    live = live_engine.live_stats(_merged_vessels(), CHOKEPOINTS)["per_chokepoint"]
    return scenario_engine.run(
        cp_id, CHOKEPOINTS, LANES, ALTERNATIVES, bundle,
        weather.safe_fetch(CHOKEPOINTS), live)


@app.get("/api/brief")
def api_brief() -> dict:
    """IDAG-briefen: automatgenererad lägesrapport ur all live-data."""
    def _compute():
        items = api_overview().get("items", [])
        return brief_engine.build(items, api_delays(), api_live(),
                                  api_markets().get("quotes", []), api_sar())

    return CACHE.get_or_fetch("brief:today", CONFIG.ttl_medium, _compute) or \
        {"headline": "Underlag saknas", "sections": []}


@app.get("/api/satellites")
def api_satellites() -> dict:
    """Satellitpositioner + nästa radarpassage per sund.

    Kopplar banmekaniken till radarlagret: Sentinel-1 är källan till våra
    SAR-detektioner, så passagetiden svarar på "när kan vi verifiera att
    sundet faktiskt är tomt?"."""
    def _compute():
        tles = satellites.safe_fetch_tles()
        return {"items": orbits_engine.positions(tles),
                "next_passes": orbits_engine.next_passes(tles, CHOKEPOINTS),
                "attribution": "Banelement: Celestrak GP-katalog",
                "ts": time.time()}

    # Positionerna åldras snabbt (7 km/s) — kort TTL; passagerna räknas om
    # i samma svep men ändras långsamt.
    return CACHE.get_or_fetch("sat:layer", 60, _compute) or         {"items": [], "next_passes": {}}


@app.get("/api/sar")
def api_sar() -> dict:
    """Sentinel-1-radardetektioner (offlinejobb skriver data/sar_detections.json).

    Fartyg som INTE sänder AIS syns ändå i SAR — komplementet där terrester
    täckning saknas (Hormuz, Afrika). Tomt tills detektionsjobbet körts."""
    try:
        return json.loads((DATA_DIR / "sar_detections.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"detections": [], "note": "inga radardetektioner ännu"}


@app.get("/api/health")
def api_health() -> dict:
    b = CACHE.get(BUNDLE_KEY)
    return {
        "bundle_ready": b is not None,
        "bundle_age_s": round(time.time() - b["generated"]) if b else None,
        "portwatch_ok": portwatch.safe_check(),
        "ais_key": ais.available,
    }


# ---------- Statiskt ----------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=CONFIG.host, port=CONFIG.port)


if __name__ == "__main__":
    main()
