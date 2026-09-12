"""Fristående AIS-insamlingsprocess.

Kör som:  python -m atlas_choke.ais_collector

Poängen: en EGEN PROCESS har sin EGEN GIL, så den CPU-tunga AIS-parsningen
konkurrerar aldrig med webbservern. Processen ansluter till aisstream, håller
senaste position per fartyg, och skriver var ~4:e sekund en färdig JSON-snapshot
ATOMISKT till SNAPSHOT_PATH. Webbservern (AisStreamSource) läser bara den filen —
den kan därför aldrig svältas ut, ens när hela världen prenumereras och 15-20k+
fartyg strömmar in.

Startas automatiskt av AisStreamSource.ensure_started() när AISSTREAM_API_KEY är
satt. Ärver miljön (nyckel + ATLAS_STRICT_TLS) från webbprocessen.
"""

from __future__ import annotations

import json
import logging
import os
import time

from .config import CONFIG
from .sources.aisstream import (
    BOUNDING_BOXES,
    MAX_VESSELS,
    SNAPSHOT_PATH,
    STALE_SECONDS,
    _category,
    _ws_connect,
    _ws_read_frame,
    _ws_send_text,
    _ws_send_pong,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s ais-collector %(levelname)s %(message)s",
)
log = logging.getLogger("atlas.ais_collector")

# Sekunder mellan snapshot-skrivningar. Vid GLOBAL prenumeration är JSON-dumpen
# själv den dyra delen (100k+ fartyg ≈ 15-25 MB) — höj till ~10-15 s då, annars
# äter serialiseringen parserns CPU och strömmen halkar efter.
WRITE_EVERY = float(os.environ.get("ATLAS_AIS_WRITE_EVERY", "4.0"))
# Backoff när servern accepterar men inte levererar. Långt som standard (snabba
# återförsök mot ett strypt konto håller strypningen vid liv) — men i globalt
# läge 2026 tappar aisstream anslutningen var 1-3:e minut och levererar fint
# däremellan; då kostar varje extra backoff-minut färskhet. Env-justerbart.
STARVED_BACKOFF_START = float(os.environ.get("ATLAS_AIS_BACKOFF", "120"))
STARVED_BACKOFF_MAX = float(os.environ.get("ATLAS_AIS_BACKOFF_MAX", "1800"))
# Hur länge en NYSS ansluten ström får vara helt tyst innan vi ger upp på den.
# Fungerande flaskhalsboxar levererar flera meddelanden per sekund.
SILENT_TIMEOUT = 90.0
EVICT_EVERY = 120.0      # rensa fartyg äldre än STALE_SECONDS


def _ingest(vessels: dict, msg: dict) -> None:
    meta = msg.get("MetaData") or {}
    mmsi = meta.get("MMSI")
    lat, lon = meta.get("latitude"), meta.get("longitude")
    if mmsi is None or lat is None or lon is None:
        return
    body = msg.get("Message") or {}
    static = body.get("ShipStaticData") or {}
    pos = body.get("PositionReport") or {}
    v = vessels.get(mmsi)
    if v is None:
        v = {}
        vessels[mmsi] = v
    v["mmsi"] = mmsi
    v["lat"] = round(lat, 5)
    v["lon"] = round(lon, 5)
    v["ts"] = time.time()
    name = (meta.get("ShipName") or "").strip()
    if name:
        v["name"] = name
    if pos:
        if pos.get("Sog") is not None:
            v["sog"] = pos.get("Sog")
        if pos.get("Cog") is not None:
            v["cog"] = pos.get("Cog")
        # NavigationalStatus (0-15): 1 = till ankars, 5 = förtöjd. Skiljer
        # RIKTIG kö (ankrad utanför ett stängt sund) från långsam gång —
        # betydligt bättre kösignal än enbart fart < 0,5 kn.
        if pos.get("NavigationalStatus") is not None:
            v["nav"] = pos.get("NavigationalStatus")
    if static:
        if static.get("Type") is not None:
            v["type_code"] = static.get("Type")
        dest = (static.get("Destination") or "").strip()
        if dest:
            v["dest"] = dest
        # DJUPGÅENDE och SKROVMÅTT. Arslanalp, Marini & Tumbarello (IMF WP/19/275)
        # visar att förändringen i rapporterat djupgående, satt i relation till
        # fartygets mått, proxar lastad vikt — det är så man går från "antal
        # fartyg" till "ton last", vilket är det ekonomin faktiskt bryr sig om.
        # Fälten kom med i varje ShipStaticData-meddelande men slängdes.
        dr = static.get("MaximumStaticDraught")
        if dr:
            try:
                d = float(dr)
                # aisstream levererar meter; äldre rådata anger decimeter.
                v["draught_m"] = round(d / 10.0 if d > 30 else d, 2)
            except (TypeError, ValueError):
                pass
        dim = static.get("Dimension") or {}
        try:
            loa = float(dim.get("A") or 0) + float(dim.get("B") or 0)
            beam = float(dim.get("C") or 0) + float(dim.get("D") or 0)
            if loa > 0:
                v["loa_m"] = round(loa, 1)
            if beam > 0:
                v["beam_m"] = round(beam, 1)
        except (TypeError, ValueError):
            pass


def _write_snapshot(vessels: dict) -> int:
    cutoff = time.time() - STALE_SECONDS
    snap = [v for v in list(vessels.values()) if v.get("ts", 0) >= cutoff]
    snap.sort(key=lambda v: v.get("ts", 0), reverse=True)   # färskast först vid taket
    items = []
    for v in snap[:MAX_VESSELS]:
        cat, css = _category(v.get("type_code"))
        items.append({"mmsi": v["mmsi"], "name": v.get("name", ""),
                      "lat": v["lat"], "lon": v["lon"],
                      "sog": v.get("sog"), "cog": v.get("cog"),
                      "nav": v.get("nav"),
                      "dest": v.get("dest", ""), "category": cat, "color": css,
                      # Bärs vidare för lastindexet. Saknas för fartyg vi bara
                      # sett positionsrapporter från — statiska meddelanden
                      # sänds glesare, var sjätte minut mot varannan sekund.
                      "draught_m": v.get("draught_m"), "loa_m": v.get("loa_m"),
                      "beam_m": v.get("beam_m")})
    # Skriv HELA API-svarsformen så webbservern kan servera filen rått, utan att
    # parsa + serialisera om 16k fartyg (~3 MB) på varje /api/layers/vessels-anrop.
    payload = json.dumps({"items": items, "count": len(items), "ts": time.time(),
                          "available": True, "source": "AISStream (live AIS)"})
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_PATH.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, SNAPSHOT_PATH)   # atomiskt byte → läsaren ser aldrig halv fil
    return len(items)


def _evict(vessels: dict) -> None:
    cutoff = time.time() - STALE_SECONDS
    for m in [m for m, v in vessels.items() if v.get("ts", 0) < cutoff]:
        del vessels[m]


def _session(vessels: dict) -> int:
    """Kör en anslutning tills den bryts. Returnerar antal MOTTAGNA meddelanden.

    Returvärdet är hela poängen: en session som slutar med noll meddelanden är
    inte ett nätverksfel utan ett SVÄLTNINGSFALL — servern accepterade nyckeln
    men skickar inget. Att skilja de två åt är skillnaden mellan att återansluta
    snabbt (rätt vid nätglapp) och att backa undan (rätt vid strypning).
    """
    s = _ws_connect()
    s.settimeout(60)
    # FilterMessageTypes (mönster från atlas-earths ais.py): utan filtret
    # skickar aisstream även bas-stations-/binärmeddelanden som bara kostar
    # parsning — med stora korridorboxar är det skillnaden mellan en lugn
    # och en mättad parser.
    _ws_send_text(s, json.dumps({"APIKey": CONFIG.aisstream_api_key,
                                 "BoundingBoxes": BOUNDING_BOXES,
                                 "FilterMessageTypes": ["PositionReport",
                                                        "ShipStaticData"]}))
    log.info("ansluten (%d box(ar), nyckel %d tecken)",
             len(BOUNDING_BOXES), len(CONFIG.aisstream_api_key))
    last_write = last_evict = last_msg = last_log = time.time()
    logged_first = False
    received = 0
    try:
        while True:
            opcode, payload = _ws_read_frame(s)
            if opcode == 0x8:            # close
                break
            # TYST ANSLUTNING. Med PONG på plats dör sessionen inte längre av
            # sig själv, vilket är rätt — men det betyder också att en svält
            # anslutning kan ligga öppen och stum i evighet utan att någonsin
            # nå felhanteringen. Utan den här kontrollen förvandlade PONG-fixen
            # ett högljutt fel till ett tyst.
            if received == 0 and time.time() - last_msg > SILENT_TIMEOUT:
                raise ConnectionError(
                    f"tyst i {int(SILENT_TIMEOUT)} s efter anslutning")
            if opcode == 0x9:            # PING → måste besvaras med PONG
                # Utan svar stänger servern efter sin keepalive-timeout (~93 s
                # uppmätt). Insamlaren loopade i evighet utan att någonsin få
                # data, och snapshotet frös i 22 timmar utan synligt fel.
                try:
                    _ws_send_pong(s, payload)
                except OSError:
                    break
                continue
            if opcode not in (0x1, 0x2) or not payload:
                continue
            try:
                msg = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                continue
            if "error" in msg:
                raise ConnectionError(str(msg.get("error"))[:120])
            _ingest(vessels, msg)
            received += 1
            now = last_msg = time.time()
            if now - last_write >= WRITE_EVERY:
                n = _write_snapshot(vessels)
                last_write = now
                if not logged_first:
                    logged_first = True
                    log.info("första snapshot skriven (%d fartyg)", n)
                # tillväxtlogg var ~2:a minut så global-lägets skala syns i loggen
                if now - last_log >= 120:
                    last_log = now
                    log.info("snapshot: %d fartyg (%d meddelanden mottagna)", n, received)
            if now - last_evict >= EVICT_EVERY:
                _evict(vessels)
                last_evict = now
    finally:
        try:
            s.close()
        except OSError:
            pass
    return received


_CAT_TO_TYPE = {"tanker": 80, "lastfartyg": 70, "passagerare": 60,
                "fiske": 30, "special": 50}


def _seed_from_snapshot(vessels: dict) -> int:
    """Återläs förra körningens snapshot så ackumulationen ÖVERLEVER omstarter.

    Utan detta nollställs 20 000+ insamlade fartyg vid varje serverstart och
    det tar ~35 min att bygga upp bilden igen. Per-fartygs-ts finns inte i
    filen — filens globala ts används, så gamla fartyg åldras ut tillsammans.
    """
    try:
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    ts = float(data.get("ts") or 0)
    if time.time() - ts > STALE_SECONDS:
        return 0
    for it in data.get("items", []):
        mmsi = it.get("mmsi")
        if mmsi is None or it.get("lat") is None:
            continue
        v = {"mmsi": mmsi, "lat": it["lat"], "lon": it["lon"], "ts": ts}
        for src_k, dst_k in (("name", "name"), ("sog", "sog"), ("cog", "cog"),
                             ("nav", "nav"), ("dest", "dest"),
                             ("draught_m", "draught_m"), ("loa_m", "loa_m"),
                             ("beam_m", "beam_m")):
            if it.get(src_k) not in (None, ""):
                v[dst_k] = it[src_k]
        tc = _CAT_TO_TYPE.get(it.get("category") or "")
        if tc:
            v["type_code"] = tc
        vessels[mmsi] = v
    return len(vessels)


def main() -> None:
    if not CONFIG.aisstream_api_key:
        log.warning("ingen AISSTREAM_API_KEY satt — insamlaren avslutas")
        return
    log.info("startar AIS-insamlare → %s", SNAPSHOT_PATH)
    vessels: dict = {}
    seeded = _seed_from_snapshot(vessels)
    if seeded:
        log.info("seedade %d fartyg från föregående snapshot", seeded)
    backoff = 2
    starved = 0
    while True:
        got = 0
        try:
            got = _session(vessels)
            log.info("strömmen stängdes efter %d meddelanden — återansluter", got)
        except Exception as exc:  # noqa: BLE001 — återhämta från alla nätfel
            log.warning("anslutning bröts (%s: %s)",
                        type(exc).__name__, str(exc)[:140])

        if got > 0:
            backoff, starved = 2, 0
            time.sleep(backoff)
            continue

        # SVÄLTNING: anslutningen accepterades men levererade inget. Att
        # återansluta snabbt gör bara saken värre — aisstream stryper "at the
        # api key and user level", och den gamla logiken hamrade var 2,5:e
        # minut i över ett dygn (302 försök, noll meddelanden). Ett övergående
        # fel gjordes därmed permanent av sin egen felhantering.
        starved += 1
        wait = min(STARVED_BACKOFF_START * (2 ** (starved - 1)), STARVED_BACKOFF_MAX)
        if starved == 1 or starved % 5 == 0:
            log.warning(
                "aisstream accepterar prenumerationen men skickar INGEN data "
                "(%d försök i rad). Nyckeln är alltså giltig — felaktiga "
                "prenumerationer stängs ute på under en sekund. Deras ström har "
                "legat tyst för alla användare sedan augusti 2026, se "
                "github.com/aisstream/issues. Kartan visar Östersjön via "
                "Digitraffic så länge. Väntar %d min.",
                starved, round(wait / 60))
        time.sleep(wait)


if __name__ == "__main__":
    main()
