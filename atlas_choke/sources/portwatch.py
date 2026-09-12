"""IMF PortWatch — dagliga chokepoint-serier ur satellit-AIS. Ingen nyckel.

Slimmad ur atlas-earth: bara chokepoint-delarna. PortWatch (portwatch.imf.org)
publicerar öppna ArcGIS feature services med dagliga transiter och lastkapacitet
(DWT) per sund sedan 2019, härledda ur satellit-AIS (~4-5 dygns fördröjning,
global täckning). Detta är prognosmotorns hela historik-ryggrad: serierna
täcker Ever Given (2021), Panama-torkan (2023) och Röda havet-krisen (2023-24).
"""

from __future__ import annotations

from ..cache import CACHE
from ..config import CONFIG
from .base import DataSource

PW_CHOKEPOINTS = ("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
                  "PortWatch_chokepoints_database/FeatureServer/0/query")
PW_DAILY_CHOKE = ("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
                  "Daily_Chokepoints_Data/FeatureServer/0/query")
PW_PORTS = ("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
            "PortWatch_ports_database/FeatureServer/0/query")
PW_DAILY_PORTS = ("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
                  "Daily_Ports_Data/FeatureServer/0/query")
PW_DISRUPTIONS = ("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
                  "portwatch_disruptions_database/FeatureServer/0/query")

VESSEL_KINDS = [
    ("vessel_count_container", "container"),
    ("vessel_count_tanker", "tanker"),
    ("vessel_count_dry_bulk", "torrlast"),
    ("vessel_count_general_cargo", "styckegods"),
    ("vessel_count_RoRo", "ro-ro"),
]


class PortWatchSource(DataSource):
    name = "portwatch"
    description = "IMF PortWatch — dagliga chokepoint-passager (satellit-AIS)"

    def _rows(self) -> list[dict]:
        """Statiska fakta för alla ~28 sund (portid, koordinater, årsvolymer)."""
        def _fetch():
            data = self.http_get_json(PW_CHOKEPOINTS, {
                "where": "1=1", "outFields": "*",
                "resultRecordCount": 100, "f": "json"})
            rows = [f["attributes"] for f in data.get("features", [])]
            return rows or None

        return CACHE.get_or_fetch("portwatch:chokepoints", CONFIG.ttl_slow, _fetch) or []

    def facts_by_name(self) -> dict[str, dict]:
        """PortWatch-portname → statiska fakta (årsvolym, per typ, industrier)."""
        out = {}
        for r in self._rows():
            nm = r.get("portname")
            if not nm:
                continue
            kinds = [{"kind": label, "count": r[key]}
                     for key, label in VESSEL_KINDS
                     if isinstance(r.get(key), (int, float)) and r[key] > 0]
            kinds.sort(key=lambda k: -k["count"])
            industries = [r.get(f"industry_top{i}") for i in (1, 2, 3)]
            out[nm] = {
                "portwatch_id": r.get("portid"),
                "lat": r.get("lat"), "lon": r.get("lon"),
                "vessels_per_year": r.get("vessel_count_total"),
                "by_kind": kinds,
                "top_industries": [i for i in industries if i],
            }
        return out

    def fetch_series(self, names: tuple[str, ...],
                     since: str | None = None) -> list[dict]:
        """Dagliga rader {date, portname, n_total, capacity, ...} för valda sund.

        ArcGIS returnerar max 1 000 rader per anrop → paginering är
        obligatorisk för fleråriga fönster. `exceededTransferLimit` är
        sanningen om fler sidor finns (sista sidan kan råka bli exakt full).
        Filtrering görs SERVERSIDAN via portname IN (...) — att hämta alla 28
        sund och kasta 17 hade kostat ~3x fler rader för samma svar.
        """
        since = since or CONFIG.portwatch_since
        fields = ("date,portid,portname,n_total,capacity,capacity_tanker,"
                  "capacity_container,capacity_dry_bulk")
        quoted = ",".join("'" + str(n).replace("'", "''") + "'" for n in names)
        where = f"date >= DATE '{since}' AND portname IN ({quoted})"

        def _fetch():
            rows, offset = [], 0
            for _ in range(80):
                data = self.http_get_json(PW_DAILY_CHOKE, {
                    "where": where, "outFields": fields,
                    "orderByFields": "date ASC", "resultRecordCount": 1000,
                    "resultOffset": offset, "f": "json"})
                feats = data.get("features") or []
                rows.extend(f["attributes"] for f in feats)
                if not feats or not data.get("exceededTransferLimit"):
                    break
                offset += len(feats)
            for r in rows:
                # epoch-ms → ISO-datum (ArcGIS levererar millisekunder)
                d = r.get("date")
                if isinstance(d, (int, float)):
                    import datetime as _dt
                    r["date"] = _dt.datetime.utcfromtimestamp(d / 1000).strftime("%Y-%m-%d")
                else:
                    r["date"] = str(d or "")[:10]
            return rows or None

        # Deterministisk cachenyckel — inbyggda hash() är slumpad per process.
        import hashlib
        tag = hashlib.sha1(("|".join(sorted(names)) + since).encode()).hexdigest()[:10]
        return CACHE.get_or_fetch(f"portwatch:series:{tag}", 43200, _fetch) or []

    # ---------- Hamnsidan (porterad från atlas-earth, trimmad) ----------

    def _port_rows(self) -> list[dict]:
        """Alla ~1 400 hamnar (paginerat — ArcGIS max ~1 000 rader/sida)."""
        def _fetch():
            rows, offset = [], 0
            while True:
                data = self.http_get_json(PW_PORTS, {
                    "where": "1=1", "outFields": "*", "f": "json",
                    "resultOffset": offset, "resultRecordCount": 1000})
                page = [f["attributes"] for f in data.get("features", [])]
                rows.extend(page)
                if len(page) < 1000:
                    break
                offset += 1000
            return rows or None

        return CACHE.get_or_fetch("portwatch:ports", CONFIG.ttl_slow, _fetch) or []

    def fetch_ports_layer(self, top_n: int = 400) -> list[dict]:
        """De `top_n` största hamnarna som kartlagerpunkter (per årsvolym)."""
        rows = []
        for r in self._port_rows():
            if not isinstance(r.get("lat"), (int, float)):
                continue
            vpy = r.get("vessel_count_total")
            rows.append({
                "portid": r.get("portid"),
                "name": r.get("portname", ""), "country": r.get("country", ""),
                "lat": r["lat"], "lon": r["lon"],
                "vessels_per_year": vpy,
                "industry": r.get("industry_top1", ""),
                "share_import": round(r["share_country_maritime_import"], 1)
                    if isinstance(r.get("share_country_maritime_import"), (int, float)) else None,
                "share_export": round(r["share_country_maritime_export"], 1)
                    if isinstance(r.get("share_country_maritime_export"), (int, float)) else None,
            })
        rows.sort(key=lambda r: -(r["vessels_per_year"] or 0))
        return rows[:top_n]

    def fetch_port_daily(self, pw_port_id: str, days: int = 120) -> list[dict]:
        """Dagliga satellit-AIS-anlöp + import/export (ton) för EN hamn."""
        def _fetch():
            data = self.http_get_json(PW_DAILY_PORTS, {
                "where": f"portid='{pw_port_id}'",
                "outFields": "date,portcalls,import,export",
                "orderByFields": "date DESC", "resultRecordCount": days,
                "f": "json"})
            rows = [f["attributes"] for f in data.get("features", [])]
            rows.reverse()
            out = []
            for r in rows:
                d = r.get("date")
                if isinstance(d, (int, float)):
                    import datetime as _dt
                    d = _dt.datetime.utcfromtimestamp(d / 1000).strftime("%Y-%m-%d")
                out.append({"date": str(d)[:10], "portcalls": r.get("portcalls"),
                            "import_t": r.get("import"), "export_t": r.get("export")})
            return out or None

        return CACHE.get_or_fetch(f"portwatch:portdaily:{pw_port_id}:{days}",
                                  43200, _fetch) or []

    def fetch_disruptions(self, days: int = 180) -> list[dict]:
        """Rapporterade hamnstörningar senaste `days` dagarna (GDACS-härledda)."""
        import time as _t
        cutoff_ms = (_t.time() - days * 86400) * 1000

        def _fetch():
            data = self.http_get_json(PW_DISRUPTIONS, {
                "where": "1=1", "outFields": "*", "f": "json",
                "orderByFields": "fromdate DESC", "resultRecordCount": 400})
            rows = []
            for f in data.get("features", []):
                a = f["attributes"]
                if not isinstance(a.get("lat"), (int, float)):
                    continue
                rows.append({"name": a.get("htmlname") or a.get("eventname", ""),
                             "kind": a.get("eventtype", ""),
                             "alertlevel": (a.get("alertlevel") or "").upper(),
                             "country": a.get("country", ""),
                             "from_ms": a.get("fromdate"), "to_ms": a.get("todate"),
                             "n_ports": a.get("n_affectedports"),
                             "ports": (a.get("affectedports") or "")[:200],
                             "lat": a["lat"], "lon": a["long"]})
            return rows or None

        rows = CACHE.get_or_fetch("portwatch:disruptions", CONFIG.ttl_medium,
                                  _fetch) or []
        return [r for r in rows if (r.get("to_ms") or r.get("from_ms") or 0) >= cutoff_ms]

    def safe_fetch_ports_layer(self, top_n: int = 400) -> list[dict]:
        return self._safe("fetch_ports_layer",
                          lambda: self.fetch_ports_layer(top_n), [])

    def safe_fetch_port_daily(self, pw_port_id: str, days: int = 120) -> list[dict]:
        return self._safe("fetch_port_daily",
                          lambda: self.fetch_port_daily(pw_port_id, days), [])

    def safe_fetch_disruptions(self, days: int = 180) -> list[dict]:
        return self._safe("fetch_disruptions",
                          lambda: self.fetch_disruptions(days), [])

    def safe_fetch_series(self, names: tuple[str, ...],
                          since: str | None = None) -> list[dict]:
        return self._safe("fetch_series",
                          lambda: self.fetch_series(names, since), [])

    def safe_facts_by_name(self) -> dict[str, dict]:
        return self._safe("facts_by_name", self.facts_by_name, {})

    def check(self) -> bool:
        return bool(self._rows())
