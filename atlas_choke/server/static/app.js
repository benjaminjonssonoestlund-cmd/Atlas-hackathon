/* ============================================================================
   ATLAS CHOKEPOINT — fokuserad glob-app.

   En funktion, byggd ordentligt: stressläget för världshandelns 11 flaskhalsar
   (IMF PortWatch satellit-AIS) på jordgloben, med episodhistorik och
   analog-baserade prisscenarier per kopplat instrument.

   Globinit och estetik ärvda från atlas-earth (Esri-satellit utan politiska
   gränser, mörk recon-ton). All lagerlogik är ny och chokepoint-specifik.
   ========================================================================== */

"use strict";

const $ = (sel) => document.querySelector(sel);

async function getJSON(url) {
  const r = await fetch(url);
  return r.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- Kartlager ----------
// Hackathon-läge: bara fraktflyg på kartan. Sätt ett lager till true för att
// slå på det igen — paneldata (ranking, analyser) laddas oavsett.
const MAP_LAYERS = {
  chokepoints: false, lanes: false, alerts: false,   // alerts = GDACS-larm (jordbävning, cyklon …) + hamnstörningar
  ports: false, vessels: false, density: false, drift: false,
  shadow: false, dark: false, sar: false, satellites: false,
  flights: "none",    // "none" (Cargojet ritas av cargojet.js) · "cargo" · "cjt" · "all"
};
function showFlight(f) {
  if (MAP_LAYERS.flights === "none") return false;
  if (MAP_LAYERS.flights === "all") return true;
  if (MAP_LAYERS.flights === "cjt") return (f.flight || "").toUpperCase().startsWith("CJT");
  return !!f.cargo;
}

// ---------- Cesium-init (samma look som atlas-earth) ----------
Cesium.Ion.defaultAccessToken = undefined;

const ESRI_IMAGERY =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";

function baseLayer() {
  const layer = new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({
    url: ESRI_IMAGERY,
    credit: new Cesium.Credit("© Esri, Maxar, Earthstar Geographics"),
    maximumLevel: 19,
  }));
  // Ljusare än atlas-earths recon-preset: kontinenterna ska synas tydligt,
  // fartygen och farlederna lyser ändå mot satellitbilden.
  layer.brightness = 0.62;
  layer.saturation = 0.62;
  layer.gamma = 0.92;
  return layer;
}

const viewer = new Cesium.Viewer("globe", {
  baseLayer: baseLayer(),
  baseLayerPicker: false, geocoder: false, timeline: false, animation: false,
  sceneModePicker: true, navigationHelpButton: false, homeButton: true,
  infoBox: false, selectionIndicator: false, fullscreenButton: false,
});
// Ingen dag/natt-skugga: hela jorden ska vara läsbar (natthalvan var svart).
viewer.scene.globe.enableLighting = false;
viewer.scene.skyAtmosphere.show = true;
viewer.camera.setView({
  // Cargojets nät: Hamilton-navet (YHM) och Nordamerika
  destination: Cesium.Cartesian3.fromDegrees(-88, 44, 8500000),
});

// Landsgränser (Natural Earth, samma fil som atlas-earth) — Esri-satelliten
// saknar politiska linjer; tunna gränser gör kontinenterna läsbara.
Cesium.GeoJsonDataSource.load("/static/countries.geojson", {
  stroke: Cesium.Color.fromCssColorString("#5f8f9c").withAlpha(0.42),
  fill: Cesium.Color.TRANSPARENT,
  strokeWidth: 1,
}).then((ds) => {
  for (const e of ds.entities.values) {
    if (e.polygon) {
      e.polygon.fill = false;
      e.polygon.outline = true;
      e.polygon.outlineColor = Cesium.Color.fromCssColorString("#5f8f9c").withAlpha(0.42);
    }
  }
  viewer.dataSources.add(ds);
}).catch((e) => console.warn("gränser:", e));

// Global handelsvägs-densitet (Världsbanken/IMF, AIS 2015-2021, CC BY 4.0):
// visar sjöfartens ådror på ALLA kontinenter — även där terrester live-AIS är
// blind (Afrika, Sydamerika, Indiska oceanen, Stilla havet).
let densityLayer = null;
function setDensity(on) {
  if (on && !densityLayer) {
    densityLayer = viewer.imageryLayers.addImageryProvider(
      new Cesium.SingleTileImageryProvider({
        url: "/static/shipping_density.png",
        rectangle: Cesium.Rectangle.fromDegrees(
          -180.015311275, -84.98735206, 180.014688725, 85.00264794),
        credit: new Cesium.Credit("Global Shipping Traffic Density © World Bank/IMF, CC BY 4.0"),
      }));
    densityLayer.alpha = 0.62;
  } else if (!on && densityLayer) {
    viewer.imageryLayers.remove(densityLayer);
    densityLayer = null;
  }
}
setDensity(MAP_LAYERS.density);
document.addEventListener("change", (e) => {
  if (e.target && e.target.id === "density-toggle") setDensity(e.target.checked);
});
// Översiktslager: ~4,9 km/pixel — på närzoom blir det pixelgröt. Göm under
// ~2 500 km kamerahöjd; togglens val respekteras när man zoomar ut igen.
viewer.camera.changed.addEventListener(() => {
  if (!densityLayer) return;
  const h = viewer.camera.positionCartographic.height;
  densityLayer.show = h > 2.5e6;
});

// ---------- Färger och nivåer ----------
const LEVEL_COLORS = {
  "normalt":    { css: "#2aff9e", cls: "lvl-normal" },
  "förhöjt":    { css: "#ffb454", cls: "lvl-elevated" },
  "allvarligt": { css: "#ff4d4d", cls: "lvl-severe" },
};
function levelInfo(level) {
  return LEVEL_COLORS[level] || { css: "#c0c8d0", cls: "" };
}
function scoreColor(score) {
  if (score == null) return "#c0c8d0";
  if (score >= 80) return "#ff4d4d";
  if (score >= 65) return "#ffb454";
  return "#2aff9e";
}

// Kända händelser → etikett på episoder (klientheuristik, datumintervall).
const KNOWN_EVENTS = [
  { from: "2020-02-15", to: "2020-09-30", label: "COVID-19-kollapsen" },
  { from: "2021-03-20", to: "2021-04-10", label: "Ever Given-blockaden" },
  { from: "2022-02-20", to: "2022-08-01", label: "Ukraina-invasionen" },
  { from: "2023-10-25", to: "2024-07-01", label: "Panama-torkan", only: ["panama"] },
  { from: "2023-11-15", to: "2024-03-01", label: "Röda havet-krisen",
    only: ["suez", "bab-el-mandeb", "cape-good-hope"] },
  { from: "2026-02-25", to: "2026-04-15", label: "Hormuz-stängningen", only: ["hormuz"] },
];
function episodeLabel(cpId, ep) {
  for (const ev of KNOWN_EVENTS) {
    if (ev.only && !ev.only.includes(cpId)) continue;
    if (ep.start >= ev.from && ep.start <= ev.to) return ev.label;
  }
  return "";
}

// ---------- Ticker ----------
async function loadTicker() {
  try {
    const d = await getJSON("/api/markets");
    if (!d.quotes || !d.quotes.length) return;
    $("#ticker").innerHTML = d.quotes
      .filter((q) => q.price != null)
      .map((q) => {
        const chg = q.change_pct;
        const cls = chg > 0 ? "up" : chg < 0 ? "down" : "";
        const arrow = chg > 0 ? "▲" : chg < 0 ? "▼" : "·";
        return `<span class="q"><b>${esc(q.name)}</b> ${q.price}` +
               ` <span class="${cls}">${arrow} ${chg == null ? "" : Math.abs(chg) + "%"}</span></span>`;
      }).join("");
  } catch (e) { console.warn("ticker:", e); }
}

// ---------- Overview → glob + ranking ----------
const cpEntities = new Map();   // id → Cesium-entity
let overview = null;
let selectedId = null;

function pulseSize(base, active) {
  if (!active) return base;
  return new Cesium.CallbackProperty(
    () => base + 4 * (0.5 + 0.5 * Math.sin(performance.now() / 320)), false);
}

function renderGlobe(items) {
  if (!MAP_LAYERS.chokepoints) return;
  for (const item of items) {
    const color = Cesium.Color.fromCssColorString(scoreColor(item.score));
    const base = item.score == null ? 8 : 9 + Math.min(9, Math.max(0, (item.score - 50) / 5));
    const existing = cpEntities.get(item.id);
    if (existing) viewer.entities.remove(existing);
    const ent = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(item.lon, item.lat),
      point: {
        pixelSize: pulseSize(base, item.active),
        color: color.withAlpha(0.92),
        outlineColor: color.withAlpha(0.28),
        outlineWidth: 7,
      },
      label: {
        text: item.inverse ? `◇ ${item.name}` : item.name,
        font: "600 13px Rajdhani, sans-serif",
        fillColor: Cesium.Color.fromCssColorString("#dfe7ec"),
        outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, -18),
      },
      properties: { cpId: item.id },
    });
    cpEntities.set(item.id, ent);
  }
  drawRerouteArcs(items);
}

// Omdirigeringsbågar: när Godahoppsudden är aktiv och Suez/Bab-el-Mandeb är
// stressade ritas trafikomläggningen ut — det är MEKANISMEN i Röda havet-krisen.
let arcEntities = [];
function drawRerouteArcs(items) {
  arcEntities.forEach((e) => viewer.entities.remove(e));
  arcEntities = [];
  const byId = Object.fromEntries(items.map((i) => [i.id, i]));
  const cape = byId["cape-good-hope"];
  if (!cape || !cape.active) return;
  for (const srcId of ["suez", "bab-el-mandeb"]) {
    const src = byId[srcId];
    if (!src || !src.active) continue;
    arcEntities.push(viewer.entities.add({
      polyline: {
        positions: Cesium.Cartesian3.fromDegreesArray(
          [src.lon, src.lat, cape.lon, cape.lat]),
        arcType: Cesium.ArcType.GEODESIC,
        width: 2.2,
        material: new Cesium.PolylineDashMaterialProperty({
          color: Cesium.Color.fromCssColorString("#66b6ff").withAlpha(0.75),
          dashLength: 18,
        }),
        clampToGround: false,
      },
    }));
  }
}

function renderRanking(items) {
  const sorted = [...items].sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  $("#ranking").classList.remove("dim");
  $("#ranking").innerHTML = sorted.map((i) => {
    const li = levelInfo(i.level);
    const tags = [];
    if (i.active) tags.push('<span class="r-tag tag-ongoing">PÅGÅR</span>');
    if (i.inverse) tags.push('<span class="r-tag tag-inverse" title="Invers nod: STIGANDE trafik = stress (omdirigeringsmottagare)">INVERS</span>');
    const lv = liveFor(i.id);
    if (lv && lv.anchored > 0)
      tags.push(`<span class="r-tag tag-anchor" title="Ankrade/förtöjda fartyg i zonen just nu (live-AIS)">⚓ ${lv.anchored}</span>`);
    const dr = delayFor(i.id);
    if (dr && dr.level !== "låg")
      tags.push(`<span class="r-tag ${dr.level === "hög" ? "tag-ongoing" : "tag-anchor"}" ` +
        `title="Förseningsrisk ${dr.level} (${dr.score}/100): väder/larm/kö — se detaljpanelen">⛈ ${dr.score}</span>`);
    return `<div class="rank-row" data-id="${esc(i.id)}">
      <span class="r-dot" style="background:${scoreColor(i.score)};box-shadow:0 0 7px ${scoreColor(i.score)}"></span>
      <span class="r-name">${esc(i.name)}</span>
      ${tags.join("")}
      <span class="r-score ${li.cls}">${i.score == null ? "–" : i.score}</span>
    </div>`;
  }).join("");
  document.querySelectorAll(".rank-row").forEach((row) => {
    row.addEventListener("click", () => selectChokepoint(row.dataset.id, true));
  });
}

async function loadOverview() {
  try {
    const d = await getJSON("/api/overview");
    if (d.building) {
      $("#ranking").innerHTML =
        'Bygger analys: 7 års satellit-AIS + prishistorik hämtas och ' +
        'event-studeras … (~1 min första gången)<div class="loadbar"></div>';
      setTimeout(loadOverview, 4000);
      return;
    }
    overview = d;
    if (!scenarioActive && !replayTimer) renderGlobe(d.items);
    renderRanking(d.items);
    // fyll scenariovalen en gång
    const sel = $("#scenario-select");
    if (sel && !sel.options.length) {
      sel.innerHTML = d.items
        .filter((i) => !i.inverse)
        .map((i) => `<option value="${esc(i.id)}">${esc(i.name)} stängs</option>`)
        .join("");
    }
  } catch (e) {
    console.warn("overview:", e);
    setTimeout(loadOverview, 6000);
  }
}

// ---------- Live-fartyg (mergad AISStream + Digitraffic) ----------
// PointPrimitiveCollection i stället för entities: en primitive-samling ritar
// 20 000+ punkter i en enda batch — entity-vägen kvävs runt ett par tusen.
const vesselPoints = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
const VESSEL_COLORS = {};
function vesselColor(css, anchored) {
  const key = css + (anchored ? "a" : "");
  if (!VESSEL_COLORS[key]) {
    const c = Cesium.Color.fromCssColorString(css || "#e8f2f6");
    VESSEL_COLORS[key] = anchored
      ? c.withAlpha(0.6)
      : c.brighten(0.25, new Cesium.Color()).withAlpha(1.0);
  }
  return VESSEL_COLORS[key];
}
// Fartygen växer när man zoomar in (NearFarScalar: nära → 3x, långt → 1x) —
// tydliga prickar i översikten, klart läsbara i regionsvyn.
const VESSEL_SCALE = new Cesium.NearFarScalar(2.0e5, 3.2, 1.6e7, 1.0);

// Dead reckoning (mönster från deck.gl:s shipping-demo + worldview): mellan
// pollarna flyttas varje fartyg i rörelse fram längs kurs (COG) med sin fart
// (SOG) — globen ser LEVANDE ut i stället för att frysa i 60 s mellan hämtningar.
let movingVessels = [];   // {pt, lat, lon, sog, cog} — bara fartyg i rörelse
let lastReckon = 0;

async function loadVessels() {
  try {
    const d = await getJSON("/api/vessels");
    const src = d.sources || {};
    $("#ais-badge").className = "badge " + (d.count > 0 ? "on" : "off");
    $("#ais-badge").textContent = d.count > 0 ? `AIS ${d.count.toLocaleString("sv-SE")}` : "AIS";
    const srcTxt = Object.entries(src).map(([k, n]) => `${n} ${k}`).join(" + ");
    $("#ais-badge").title = d.count > 0
      ? `Unika fartyg hörda senaste 3 h: ${srcTxt}. Fartyg i rörelse extrapoleras längs kurs/fart (dead reckoning); halvgenomskinlig punkt = ankrad/förtöjd. Källor: AISStream (global), Fintraffic CC BY 4.0 (Östersjön), BarentsWatch NLOD (valfri), egen AIS-catcher-station (valfri).`
      : "Live-AIS inaktiv — analysen bygger på PortWatch och påverkas inte";
    vesselPoints.removeAll();
    movingVessels = [];
    // Adaptiv punktstorlek: vid global skala (50k+) krymper punkterna så att
    // farlederna läses som linjer i stället för att smeta ihop till ytor.
    const n = (d.items || []).length;
    const szMove = n > 50000 ? 2.2 : n > 20000 ? 2.7 : 3.2;
    const szAnch = n > 50000 ? 1.7 : n > 20000 ? 2.1 : 2.5;
    for (const v of MAP_LAYERS.vessels ? d.items || [] : []) {
      const anchored = v.nav === 1 || v.nav === 5 || (v.nav == null && (v.sog || 0) < 0.5);
      const pt = vesselPoints.add({
        position: Cesium.Cartesian3.fromDegrees(v.lon, v.lat),
        pixelSize: anchored ? szAnch : szMove,
        color: vesselColor(v.color, anchored),
        scaleByDistance: VESSEL_SCALE,
        id: { vessel: v },   // pick() returnerar detta → klickbart fartygskort
      });
      if (!anchored && (v.sog || 0) >= 0.5 && v.cog != null) {
        movingVessels.push({ pt, lat: v.lat, lon: v.lon, sog: v.sog, cog: v.cog });
      }
    }
    lastReckon = performance.now();
  } catch (e) { console.warn("vessels:", e); }
}

// 1 Hz räcker — mjukt nog för ögat, billigt nog för 20k+ punkter.
setInterval(() => {
  if (!movingVessels.length) return;
  const now = performance.now();
  const dt = (now - lastReckon) / 1000;
  lastReckon = now;
  for (const m of movingVessels) {
    const distKm = m.sog * 0.000514 * dt;          // 1 knop ≈ 0,000514 km/s
    const rad = (m.cog * Math.PI) / 180;
    m.lat += (distKm * Math.cos(rad)) / 111.32;
    m.lon += (distKm * Math.sin(rad)) / (111.32 * Math.cos((m.lat * Math.PI) / 180) || 1);
    m.pt.position = Cesium.Cartesian3.fromDegrees(m.lon, m.lat);
  }
}, 1000);

// ---------- Handelsvägar + förseningsrisk ----------
const RISK_COLORS = { "låg": "#2aa9ff", "förhöjd": "#ffb454", "hög": "#ff4d4d" };
let delayData = null;          // /api/delays-svaret
let laneEntities = [];
let alertEntities = [];

function drawLanes(lanes) {
  laneEntities.forEach((e) => viewer.entities.remove(e));
  laneEntities = [];
  if (!MAP_LAYERS.lanes) return;
  for (const lane of lanes) {
    const color = Cesium.Color.fromCssColorString(
      RISK_COLORS[lane.risk_level] || "#2aa9ff");
    const width = Math.max(1.6, Math.sqrt(lane.teu_share_pct || 1) * 1.5);
    const ent = viewer.entities.add({
      polyline: {
        positions: Cesium.Cartesian3.fromDegreesArray(lane.waypoints.flat()),
        width: lane.risk_level === "hög" ? width + 1.5 : width,
        material: lane.risk_level === "låg"
          ? new Cesium.PolylineDashMaterialProperty({
              color: color.withAlpha(0.55), dashLength: 16 })
          : new Cesium.PolylineGlowMaterialProperty({
              color: color.withAlpha(0.85), glowPower: 0.25 }),
        clampToGround: false,
      },
      properties: { laneId: lane.id },
    });
    laneEntities.push(ent);
  }
}

function selectLane(laneId) {
  const lane = (delayData?.lanes || []).find((l) => l.id === laneId);
  if (!lane) return;
  selectedId = null;
  $("#detail").classList.remove("hidden");
  const viaRows = (lane.via || []).map((cpId) => {
    const cp = (overview?.items || []).find((i) => i.id === cpId);
    const dr = delayFor(cpId);
    const color = dr ? (RISK_COLORS[dr.level] || "#2aa9ff") : "#c0c8d0";
    return `<div class="rank-row" data-id="${esc(cpId)}">
      <span class="r-dot" style="background:${color};box-shadow:0 0 7px ${color}"></span>
      <span class="r-name">${esc(cp ? cp.name : cpId)}</span>
      <span class="r-score" style="color:${color}">${dr ? dr.score : "–"}</span>
    </div>`;
  }).join("");
  const worst = (overview?.items || []).find((i) => i.id === lane.worst_via);
  $("#detail-content").innerHTML = `
    <h2>${esc(lane.name)}</h2>
    <div class="hint">${lane.teu_share_pct ? `~${lane.teu_share_pct} % av global container-TEU.` : ""}
    Ledens risk = värsta flaskhalsens risk längs vägen.</div>
    <div class="big-score" style="color:${RISK_COLORS[lane.risk_level] || "#2aa9ff"}">${lane.risk_score}<small> /100 · ${esc(lane.risk_level)}</small></div>
    ${lane.delay_days ? `<div class="fc-note active"><b>Bedömd försening: +${lane.delay_days[0]}–${lane.delay_days[1]} dygn</b>${worst ? " (värsta länk: " + esc(worst.name) + ")" : ""}</div>` : ""}
    <h4>Flaskhalsar längs leden (förseningsrisk)</h4>
    ${viaRows || '<p class="hint">Inga bevakade flaskhalsar på leden.</p>'}
    <p class="hint">Klicka en flaskhals för full analys.</p>`;
  document.querySelectorAll("#detail-content .rank-row").forEach((row) => {
    row.addEventListener("click", () => selectChokepoint(row.dataset.id, true));
  });
}

// AIS NavigationalStatus → svensk etikett (samma tabell som atlas-earth)
const NAV_SV = { 0: "under gång", 1: "till ankars", 2: "ej under kontroll",
  3: "begränsad manöver", 4: "begränsad av djupgående", 5: "förtöjd",
  6: "på grund", 7: "fiskar", 8: "under segel" };

function selectVessel(v) {
  selectedId = null;
  $("#detail").classList.remove("hidden");
  const anchored = v.nav === 1 || v.nav === 5;
  $("#detail-content").innerHTML = `
    <h2>${esc(v.name || "MMSI " + v.mmsi)}</h2>
    <div class="hint">${esc(v.category || "okänt fartyg")}${anchored ? " · ⚓ " + esc(NAV_SV[v.nav]) : ""}</div>
    <table style="margin-top:10px">
      <tr><td>MMSI</td><td class="val">${esc(v.mmsi)}</td></tr>
      <tr><td>Fart / kurs</td><td class="val">${v.sog ?? "–"} kn / ${v.cog != null ? Math.round(v.cog) + "°" : "–"}</td></tr>
      <tr><td>Status</td><td class="val">${esc(NAV_SV[v.nav] ?? "okänd")}</td></tr>
      ${v.dest ? `<tr><td>Destination (AIS)</td><td class="val">${esc(v.dest)}</td></tr>` : ""}
      ${v.draught_m ? `<tr><td>Djupgående</td><td class="val">${v.draught_m} m</td></tr>` : ""}
      ${v.loa_m ? `<tr><td>Längd × bredd</td><td class="val">${v.loa_m} × ${v.beam_m ?? "?"} m</td></tr>` : ""}
      <tr><td>Position</td><td class="val">${v.lat.toFixed(3)}, ${v.lon.toFixed(3)}</td></tr>
      ${v.watchlist ? `<tr><td>Bevakningslista</td><td class="val">
        <span class="r-tag tag-ongoing">⚑ SKUGGFLOTTA</span>` +
        (v.watchlist.imo ? ` IMO ${esc(v.watchlist.imo)}` : "") + `</td></tr>` : ""}
      ${v.flag ? `<tr><td>Flaggstat</td><td class="val">${esc(v.flag.country)}` +
        (v.flag.shadow_risk ? ' <span class="r-tag tag-ongoing">HÖGRISK</span>'
         : v.flag.foc ? ' <span class="r-tag tag-anchor">FOC</span>' : "") + `</td></tr>` : ""}
      <tr><td>Källa</td><td class="val">${esc(v.src || "aisstream")}</td></tr>
    </table>
    <p class="hint" style="margin-top:8px">Positionen är senast hörda AIS-rapport;
    fartyg i rörelse extrapoleras längs kurs/fart mellan uppdateringar.</p>`;
}

function drawAlerts(alerts) {
  alertEntities.forEach((e) => viewer.entities.remove(e));
  alertEntities = [];
  if (!MAP_LAYERS.alerts) return;
  for (const a of alerts || []) {
    const red = a.alertlevel === "RED";
    alertEntities.push(viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(a.lon, a.lat),
      point: {
        pixelSize: red ? 11 : 8,
        color: Cesium.Color.fromCssColorString(red ? "#ff2e63" : "#ff8c42").withAlpha(0.9),
        outlineColor: Cesium.Color.fromCssColorString(red ? "#ff2e63" : "#ff8c42").withAlpha(0.25),
        outlineWidth: 9,
      },
      label: {
        text: `🌀 ${a.name}`,
        font: "600 12px Rajdhani, sans-serif",
        fillColor: Cesium.Color.fromCssColorString("#ffd0d0"),
        outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, -16),
      },
    }));
  }
}

async function loadDelays() {
  try {
    delayData = await getJSON("/api/delays");
    drawLanes(delayData.lanes || []);
    drawAlerts(delayData.alerts || []);
    if (overview) renderRanking(overview.items);
    if (selectedId) updateDelaySection(selectedId);
  } catch (e) { console.warn("delays:", e); }
}

function delayFor(id) {
  return (delayData && delayData.per_chokepoint && delayData.per_chokepoint[id]) || null;
}

function updateDelaySection(id) {
  const box = $("#delay-section");
  if (!box) return;
  const dr = delayFor(id);
  if (!dr) { box.innerHTML = '<p class="hint">Riskdata laddas …</p>'; return; }
  const color = RISK_COLORS[dr.level] || "#2aa9ff";
  const reasons = (dr.reasons || []).map((r) =>
    `<div class="ep-row"><span class="e-z">${r.pts ? "+" + r.pts : "→"}</span>` +
    `<span class="e-label">${esc(r.txt)}</span></div>`).join("") ||
    '<p class="hint">Inga aktiva risksignaler — väder, larm och kö är normala.</p>';
  const delay = dr.delay_days
    ? `<div class="fc-note active"><b>Bedömd försening: +${dr.delay_days[0]}–${dr.delay_days[1]} dygn</b></div>` : "";
  const wx = dr.weather || {};
  const wxTxt = [
    wx.gust_ms != null ? `byar nu ${wx.gust_ms} m/s` : "",
    wx.gust_max48_ms != null ? `max 48 h ${wx.gust_max48_ms} m/s` : "",
    wx.wave_max48_m != null ? `våg max ${wx.wave_max48_m} m` : "",
  ].filter(Boolean).join(" · ");
  box.innerHTML = `
    <div class="big-score" style="color:${color}">${dr.score}<small> /100 · ${esc(dr.level)}</small></div>
    <div class="gauge-wrap"><div class="gauge-bar">
      <div class="gauge-fill" style="width:${dr.score}%;background:${color};box-shadow:0 0 9px ${color}"></div>
    </div><div class="gauge-meta"><span>${esc(wxTxt || "väderdata saknas")}</span><span>Open-Meteo + GDACS + AIS</span></div></div>
    ${delay}${reasons}`;
}

// ---------- Drivande & loitrande fartyg → råvaror ----------
const DRIFT_COLORS = {
  "ej under kontroll": "#ff2e63", "begränsad manöver": "#ff8c42",
  "misstänkt loitering": "#b46bff", "loitering": "#b46bff",
  "omlastning": "#4dc3ff", "navvarning": "#ffb454",
};
let driftData = null;
let driftEntities = [];

function drawDrift(d) {
  driftEntities.forEach((e) => viewer.entities.remove(e));
  driftEntities = [];
  if (!MAP_LAYERS.drift) return;
  const add = (obs, label, big) => {
    const css = DRIFT_COLORS[obs.label || obs.kind] || "#b46bff";
    const color = Cesium.Color.fromCssColorString(css);
    driftEntities.push(viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(obs.lon, obs.lat),
      point: {
        pixelSize: big ? 7 : 5,
        color: color.withAlpha(0.95),
        outlineColor: color.withAlpha(0.22),
        outlineWidth: big ? 8 : 6,
      },
      label: label ? {
        text: label,
        font: "600 11px Rajdhani, sans-serif",
        fillColor: color.brighten(0.4, new Cesium.Color()),
        outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, -13),
        // Bara vid närzoom: "begränsad manöver" är legitim vardag för hundratals
        // mudderverk/bogserare — på regionzoom dränkte etiketterna hela kartan.
        distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 2.2e6),
      } : undefined,
    }));
  };
  for (const o of (d.own || []).slice(0, 250)) {
    const hard = o.label !== "misstänkt loitering";
    add(o, hard ? `⚠ ${o.name || o.mmsi} · ${o.label}` : "", hard);
  }
  for (const o of (d.gfw_loitering || []).slice(0, 250)) add(o, "", false);
  for (const o of (d.gfw_encounters || []).slice(0, 120)) add(o, "⇄", false);
  for (const o of (d.nga_warnings || []).slice(0, 80)) add(o, `⚠ ${o.id}`, true);
}

function renderDriftPanel(d) {
  const box = $("#drift-panel");
  if (!box) return;
  const t = d.totals || {};
  const gfwNote = d.gfw_available
    ? `${t.gfw_loitering ?? 0} loitering + ${t.gfw_encounters ?? 0} omlastningar (GFW, 7 d)`
    : "GFW avstängd — sätt GFW_API_TOKEN (gratis) för satellit-loitering";
  const rows = Object.entries(d.per_chokepoint || {})
    .map(([id, e]) => ({ id, e, act: e.own + e.loitering + e.encounters + e.warnings }))
    .filter((r) => r.act > 0)
    .sort((a, b) => b.act - a.act)
    .slice(0, 6)
    .map((r) => {
      const cp = (overview?.items || []).find((i) => i.id === r.id);
      const notes = (r.e.market_notes || [])
        .map((n) => `<div class="drift-note">${esc(n)}</div>`).join("");
      return `<div class="rank-row" data-id="${esc(r.id)}">
        <span class="r-dot" style="background:#b46bff;box-shadow:0 0 7px #b46bff"></span>
        <span class="r-name">${esc(cp ? cp.name : r.id)}</span>
        <span class="r-score">${r.act}</span>
      </div>${notes}`;
    }).join("") || '<p class="hint">Inga drivande/loitrande fartyg nära flaskhalsarna just nu.</p>';
  box.innerHTML = `
    <div class="mini"><b>${t.own ?? 0}</b> manöverstörda/zon-loitrande i egen AIS
    (varav <b>${t.own_tankers ?? 0}</b> tankers) · <b>${t.nga_warnings ?? 0}</b>
    NGA-varningar · ${esc(gfwNote)}</div>${rows}`;
  document.querySelectorAll("#drift-panel .rank-row").forEach((row) => {
    row.addEventListener("click", () => selectChokepoint(row.dataset.id, true));
  });
}

function updateDriftSection(id) {
  const box = $("#drift-section");
  if (!box) return;
  const e = driftData?.per_chokepoint?.[id];
  if (!e) {
    box.innerHTML = '<p class="hint">Inga drivande/loitrande fartyg i zonen (≤250 km) just nu.</p>';
    return;
  }
  const notes = (e.market_notes || [])
    .map((n) => `<div class="drift-note">${esc(n)}</div>`).join("");
  box.innerHTML = `
    <table>
      <tr><td>Manöverstörda/zon-loitrande (egen AIS)</td><td class="val">${e.own}</td></tr>
      <tr><td>Loitering-events (GFW, 7 d)</td><td class="val">${e.loitering}</td></tr>
      <tr><td>Omlastningar till havs (GFW, 7 d)</td><td class="val">${e.encounters}</td></tr>
      <tr><td>Sjövarningar drivande fartyg (NGA)</td><td class="val">${e.warnings}</td></tr>
      <tr><td>Varav tankers totalt</td><td class="val">${e.tankers_total}</td></tr>
    </table>
    ${notes || '<p class="hint">Aktivitet utan tydlig råvarukoppling ännu.</p>'}`;
}

async function loadDrift() {
  try {
    driftData = await getJSON("/api/drift");
    drawDrift(driftData);
    renderDriftPanel(driftData);
    if (selectedId) updateDriftSection(selectedId);
  } catch (e) { console.warn("drift:", e); }
}

// ---------- Hamnar (PortWatch ~400 största) + störningar ----------
let portEntities = [];
let disruptionEntities = [];
let portsById = {};

async function loadPorts() {
  try {
    const d = await getJSON("/api/ports");
    portEntities.forEach((e) => viewer.entities.remove(e));
    disruptionEntities.forEach((e) => viewer.entities.remove(e));
    portEntities = []; disruptionEntities = []; portsById = {};
    for (const p of d.items || []) {
      if (!p.portid) continue;
      portsById[p.portid] = p;
      if (!MAP_LAYERS.ports) continue;
      const size = 3 + Math.min(5, Math.sqrt((p.vessels_per_year || 0) / 4000));
      const hot = p.live_inbound && p.live_inbound.inbound > 0;
      portEntities.push(viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(p.lon, p.lat),
        point: {
          pixelSize: size,
          color: Cesium.Color.fromCssColorString(hot ? "#38d5ff" : "#2a8fb0")
            .withAlpha(hot ? 0.95 : 0.7),
          outlineColor: Cesium.Color.fromCssColorString("#38d5ff").withAlpha(0.2),
          outlineWidth: hot ? 5 : 2,
        },
        label: {
          text: p.name,
          font: "500 11px Rajdhani, sans-serif",
          fillColor: Cesium.Color.fromCssColorString("#a8d8e8"),
          outlineColor: Cesium.Color.BLACK, outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -12),
          // etiketter bara vid inzoomning — 400 namn i översikten vore gröt
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 5.5e6),
        },
        properties: { portId: p.portid },
      }));
    }
    for (const dis of MAP_LAYERS.alerts ? d.disruptions || [] : []) {
      const red = dis.alertlevel === "RED";
      disruptionEntities.push(viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(dis.lon, dis.lat),
        point: {
          pixelSize: red ? 9 : 7,
          color: Cesium.Color.fromCssColorString(red ? "#ff2e63" : "#ff8c42").withAlpha(0.85),
          outlineColor: Cesium.Color.fromCssColorString("#ff8c42").withAlpha(0.22),
          outlineWidth: 7,
        },
        label: {
          text: `⚠ ${dis.name}`,
          font: "600 11px Rajdhani, sans-serif",
          fillColor: Cesium.Color.fromCssColorString("#ffd8b0"),
          outlineColor: Cesium.Color.BLACK, outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -13),
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 8e6),
        },
      }));
    }
  } catch (e) { console.warn("ports:", e); }
}

async function selectPort(portId) {
  const p = portsById[portId];
  if (!p) return;
  selectedId = null;
  $("#detail").classList.remove("hidden");
  $("#detail-content").innerHTML =
    '<div class="dim" style="padding:30px 0">Hämtar hamndata …<div class="loadbar"></div></div>';
  let series = [];
  try {
    series = (await getJSON("/api/port/" + encodeURIComponent(portId) + "/daily")).series || [];
  } catch (e) { /* panelen visar ändå fakta */ }
  const li = p.live_inbound;
  const cats = li ? Object.entries(li.by_category)
    .map(([k, n]) => `<span>${esc(k)} ${n}</span>`).join("") : "";
  const last = series[series.length - 1];
  $("#detail-content").innerHTML = `
    <h2>${esc(p.name)}</h2>
    <div class="hint">${esc(p.country)}${p.industry ? " · toppindustri: " + esc(p.industry) : ""}</div>
    ${li ? `
    <h4>På väg hit just nu (AIS-destinationer)</h4>
    <div class="big-score" style="color:#38d5ff">${li.inbound}<small> fartyg · ${li.underway} i rörelse</small></div>
    <div class="fact-kinds">${cats}</div>
    <p class="hint">Golv, inte totalräkning — bara fartyg som deklarerat destination räknas.</p>`
    : '<p class="hint" style="margin-top:8px">Inga destinationsdeklarerade fartyg mot hamnen i snapshotet just nu.</p>'}

    <h4>Dagliga anlöp (satellit-AIS, 120 d)</h4>
    <canvas class="spark-canvas" id="port-spark"></canvas>
    <div class="spark-cap"><span>${last ? "senast: " + last.portcalls + " anlöp (" + esc(last.date) + ")" : "ingen serie"}</span>
    <span>PortWatch släpar ~5 dygn</span></div>

    <h4>Om hamnen (PortWatch, 7-årsfakta)</h4>
    <table>
      <tr><td>Fartygsanlöp/år</td><td class="val">${fmtInt(p.vessels_per_year)}</td></tr>
      ${p.share_import != null ? `<tr><td>Andel av landets sjöimport</td><td class="val">${p.share_import}%</td></tr>` : ""}
      ${p.share_export != null ? `<tr><td>Andel av landets sjöexport</td><td class="val">${p.share_export}%</td></tr>` : ""}
      ${last && last.import_t != null ? `<tr><td>Import senaste dygnet</td><td class="val">${fmtInt(last.import_t)} t</td></tr>` : ""}
      ${last && last.export_t != null ? `<tr><td>Export senaste dygnet</td><td class="val">${fmtInt(last.export_t)} t</td></tr>` : ""}
    </table>`;
  const spark = series.filter((r) => r.portcalls != null)
    .map((r) => ({ date: r.date, value: r.portcalls }));
  if (spark.length > 1) sparkline($("#port-spark"), spark, null);
}

// ---------- TIDSMASKIN: spela upp 2019 → idag ----------
// Veckosamplad riktad z per sund (ur bundeln) → globens punkter färgas om
// vecka för vecka. Historiens kriser tänds i tur ordning: COVID, Ever Given,
// Ukraina/Bosporen, Panama-torkan, Röda havet, Hormuz.
const REPLAY_MS = 110;             // ms per vecka
let replayData = null;             // {cpId: [[date, z], ...]}
let replayTimer = null;
let replayIdx = 0;

async function ensureReplayData() {
  if (replayData) return replayData;
  const ids = (overview?.items || []).map((i) => i.id);
  const out = {};
  await Promise.all(ids.map(async (id) => {
    try {
      const cp = await getJSON("/api/chokepoint/" + encodeURIComponent(id));
      if (cp.z_weekly) out[id] = cp.z_weekly;
    } catch (e) { /* sundet utelämnas ur uppspelningen */ }
  }));
  replayData = out;
  return out;
}

function replayScoreColor(z) {
  const score = Math.max(0, Math.min(100, 50 + 10 * z));
  return scoreColor(score);
}

function replayApply(idx) {
  let label = "";
  for (const [id, series] of Object.entries(replayData || {})) {
    const point = series[Math.min(idx, series.length - 1)];
    if (!point) continue;
    label = point[0];
    const ent = cpEntities.get(id);
    if (!ent) continue;
    const z = point[1];
    const css = replayScoreColor(z);
    const c = Cesium.Color.fromCssColorString(css);
    ent.point.color = c.withAlpha(0.92);
    ent.point.outlineColor = c.withAlpha(0.28);
    ent.point.pixelSize = 9 + Math.min(11, Math.max(0, z * 3.5));
  }
  const bar = $("#replay-date");
  if (bar) bar.textContent = label || "";
  const known = REPLAY_EVENTS.find((e) => label >= e.from && label <= e.to);
  const note = $("#replay-event");
  if (note) note.textContent = known ? known.label : "";
}

const REPLAY_EVENTS = [
  { from: "2020-02-15", to: "2020-09-30", label: "🦠 COVID-19 — global handelskollaps" },
  { from: "2021-03-23", to: "2021-04-15", label: "🚢 Ever Given blockerar Suez" },
  { from: "2022-02-24", to: "2022-08-01", label: "⚔ Ukraina-invasionen — Bosporen stryps" },
  { from: "2023-10-25", to: "2024-07-01", label: "🌵 Panama-torkan — djupgåendegränser" },
  { from: "2023-11-15", to: "2024-06-01", label: "🎯 Röda havet-krisen — omväg runt Afrika" },
  { from: "2026-02-25", to: "2026-12-31", label: "🛑 Hormuz stängt" },
];

async function replayStart() {
  await ensureReplayData();
  const maxLen = Math.max(...Object.values(replayData).map((s) => s.length), 0);
  if (!maxLen) return;
  // Lederna bär NUTIDENS risk — dölj dem under uppspelningen så att bara
  // sundens historiska tillstånd talar (annars ser 2019 rött ut).
  laneEntities.forEach((e) => { e.show = false; });
  $("#replay-bar").classList.remove("hidden");
  replayIdx = 0;
  clearInterval(replayTimer);
  replayTimer = setInterval(() => {
    replayApply(replayIdx);
    const pct = (replayIdx / (maxLen - 1)) * 100;
    $("#replay-fill").style.width = pct.toFixed(1) + "%";
    if (++replayIdx >= maxLen) replayStop(true);
  }, REPLAY_MS);
}

function replayStop(finished) {
  clearInterval(replayTimer);
  replayTimer = null;
  if (!finished) $("#replay-bar").classList.add("hidden");
  laneEntities.forEach((e) => { e.show = true; });
  if (overview) renderGlobe(overview.items);   // tillbaka till nuläget
}

// ---------- SCENARIO: vad händer om ett sund stängs? ----------
let scenarioActive = null;

async function runScenario(cpId) {
  const s = await getJSON("/api/scenario/" + encodeURIComponent(cpId));
  if (s.error) return;
  scenarioActive = s;
  drawLanes(s.lanes || []);
  // globens punkter får scenariots riskfärger
  for (const [id, r] of Object.entries(s.per_chokepoint || {})) {
    const ent = cpEntities.get(id);
    if (!ent) continue;
    const c = Cesium.Color.fromCssColorString(RISK_COLORS[r.level] || "#2aa9ff");
    ent.point.color = c.withAlpha(0.92);
    ent.point.outlineColor = c.withAlpha(0.3);
    ent.point.pixelSize = 9 + r.score / 8;
  }
  $("#detail").classList.remove("hidden");
  const lanes = (s.affected_lanes || []).map((l) =>
    `<div class="ep-row"><span class="e-label">${esc(l.name)}</span>
     <span class="e-z" style="color:${RISK_COLORS[l.risk_level]}">${esc(l.risk_level)}</span></div>`).join("");
  const inst = (s.instruments || []).map((i) => {
    const a = (i.analog || {})["10"] || (i.analog || {})["20"];
    if (!a) return "";
    return `<div class="fc-inst"><div class="fc-head">
      <span class="f-sym">${esc(i.symbol)}</span><span class="f-name">${esc(i.name)}</span>
      <span class="f-quote ${a.median > 0 ? "up" : "down"}">${a.median > 0 ? "+" : ""}${a.median} %</span></div>
      <div class="fc-why">${esc(i.why)} · historiskt utfall +10 hd, band [${a.p10}, ${a.p90}], N=${a.n}</div></div>`;
  }).join("");
  $("#detail-content").innerHTML = `
    <h2>Scenario: ${esc(s.chokepoint.name)} stängt <span class="sim-tag">SIMULERING</span></h2>
    <div class="hint">${esc(s.chokepoint.trade_note || "")}</div>
    ${s.alternative ? `<div class="fc-note active"><b>Omväg:</b> ${esc(s.alternative.route)}
      (+${s.alternative.delay_days[0]}–${s.alternative.delay_days[1]} dygn).
      ${esc(s.alternative.note || "")}</div>` : ""}
    <div class="big-score" style="color:#ff4d4d">${s.teu_at_risk_pct}<small> % av global container-TEU på berörda leder</small></div>
    <h4>Berörda handelsvägar (${(s.affected_lanes || []).length})</h4>
    ${lanes || '<p class="hint">Inga bevakade leder passerar sundet.</p>'}
    <h4>Historiskt prisutfall vid stress i detta sund</h4>
    ${inst || `<p class="hint">Sundet har ${s.n_episodes} historiska episoder — för få analoger för prisband.</p>`}
    <p class="hint">Simulering: sundet tvingas till full stress och samma motorer
    som live-analysen räknar om risk, förseningar och leder. Prisutfallen är
    <b>faktiska historiska analoger</b>, inte påhittade siffror.</p>
    <button id="scenario-clear" style="width:100%;margin-top:10px">Återgå till live-läget</button>`;
  $("#scenario-clear").addEventListener("click", clearScenario);
}

function clearScenario() {
  scenarioActive = null;
  if (overview) renderGlobe(overview.items);
  if (delayData) drawLanes(delayData.lanes || []);
  $("#detail").classList.add("hidden");
}

// ---------- IDAG-brief ----------
async function showBrief() {
  const box = $("#brief-overlay");
  box.classList.remove("hidden");
  $("#brief-body").innerHTML = '<div class="dim">Sammanställer lägesbild …<div class="loadbar"></div></div>';
  try {
    const b = await getJSON("/api/brief");
    $("#brief-body").innerHTML = `
      <h2>${esc(b.headline)}</h2>
      ${(b.sections || []).map((s) => `
        <h4>${esc(s.title)}</h4>
        ${s.rows.map((r) => `<div class="brief-row">${esc(r)}</div>`).join("")}`).join("")}
      <p class="hint" style="margin-top:14px">${esc(b.footer || "")}</p>`;
  } catch (e) {
    $("#brief-body").innerHTML = '<p class="hint">Kunde inte bygga briefen.</p>';
  }
}

// ---------- Live flyg (ADS-B) — substitutionskanalen för störd sjöfrakt ----------
// Ritas på VERKLIG höjd över globen: fraktflyg gult/varmt, övrig trafik svagt
// blått. Att se planen sväva ovanför fartygen gör transportmodalitets-
// berättelsen visuell — "godset flyttar från sjö till luft".
const planePoints = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
const PLANE_SCALE = new Cesium.NearFarScalar(3.0e5, 2.6, 2.0e7, 0.9);
let airData = null;

async function loadFlights() {
  try {
    const d = await getJSON("/api/flights");
    airData = d;
    planePoints.removeAll();
    for (const f of d.items || []) {
      if (!showFlight(f)) continue;
      const altM = (f.alt_ft || 30000) * 0.3048;
      planePoints.add({
        position: Cesium.Cartesian3.fromDegrees(f.lon, f.lat, altM),
        pixelSize: f.cargo ? 5.0 : 2.6,
        color: f.cargo
          ? Cesium.Color.fromCssColorString("#ffd24d").withAlpha(0.95)
          : Cesium.Color.fromCssColorString("#8fb8ff").withAlpha(0.45),
        scaleByDistance: PLANE_SCALE,
        id: { flight: f },
      });
    }
    const t = d.totals || {};
    if (t.flights) {
      console.info(`ADS-B: ${t.flights} flyg, ${t.cargo} fraktflyg ` +
                   `(${Math.round((t.cargo_share || 0) * 100)} %)`);
    }
    if (selectedId) updateAirSection(selectedId);
  } catch (e) { console.warn("flights:", e); }
}

function updateAirSection(id) {
  const box = $("#air-section");
  if (!box) return;
  const a = airData?.per_chokepoint?.[id];
  if (!a) {
    box.innerHTML = '<p class="hint">Ingen ADS-B-täckning i zonen just nu.</p>';
    return;
  }
  const flag = a.substitution
    ? '<span class="r-tag tag-ongoing">SUBSTITUTION</span>'
    : a.elevated ? '<span class="r-tag tag-anchor">FÖRHÖJD FRAKT</span>' : "";
  box.innerHTML = `
    <table>
      <tr><td>Flyg i zonen (250 nm)</td><td class="val">${a.flights}</td></tr>
      <tr><td>Varav fraktflyg ✈</td><td class="val">${a.cargo} (${Math.round(a.cargo_share * 100)} %) ${flag}</td></tr>
    </table>
    ${a.top_cargo?.length ? `<div class="fact-kinds">${a.top_cargo.map((c) => `<span>${esc(c)}</span>`).join("")}</div>` : ""}
    ${a.note ? `<div class="drift-note">${esc(a.note)}</div>` : ""}`;
}

// ---------- Skuggflotta: flaggstat bland tankers vid oljesunden ----------
let shadowData = null;
let watchEntities = [];
async function loadShadow() {
  try {
    shadowData = await getJSON("/api/shadowfleet");
    watchEntities.forEach((e) => viewer.entities.remove(e));
    watchEntities = [];
    for (const v of MAP_LAYERS.shadow ? shadowData.watchlist_live || [] : []) {
      const c = Cesium.Color.fromCssColorString("#ff2e63");
      watchEntities.push(viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(v.lon, v.lat),
        point: { pixelSize: 8, color: c.withAlpha(0.95),
                 outlineColor: c.withAlpha(0.3), outlineWidth: 9 },
        label: {
          text: `⚑ ${v.name || v.mmsi}`,
          font: "700 11px Rajdhani, sans-serif",
          fillColor: Cesium.Color.fromCssColorString("#ffd0dc"),
          outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -14),
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 4.0e6),
        },
      }));
    }
    const box = $("#watch-panel");
    if (box) {
      const rows = (shadowData.watchlist_live || []).slice(0, 8).map((v) =>
        `<div class="rank-row"><span class="r-dot" style="background:#ff2e63;box-shadow:0 0 7px #ff2e63"></span>
         <span class="r-name">${esc(v.name || v.mmsi)}${v.flag ? " · " + esc(v.flag) : ""}</span>
         <span class="r-score">${esc(v.category || "")}</span></div>`).join("");
      box.innerHTML = `<div class="mini"><b>${shadowData.watchlist_total ?? 0}</b> av
        ${(shadowData.watchlist_size ?? 0).toLocaleString("sv-SE")} bevakade fartyg
        syns i live-AIS just nu</div>${rows ||
        '<p class="hint">Inga bevakade fartyg i live-bilden.</p>'}`;
    }
    if (selectedId) updateShadowSection(selectedId);
  } catch (e) { console.warn("shadowfleet:", e); }
}

function updateMilSection(id) {
  const box = $("#mil-section");
  if (!box) return;
  const m = airData?.military_near?.[id];
  if (!m) { box.innerHTML = '<p class="hint">Inget militärflyg i närområdet just nu.</p>'; return; }
  box.innerHTML = `
    <div class="mini"><b>${m.count}</b> militära flygplan, närmast <b>${m.closest_km} km</b></div>
    <div class="fact-kinds">${m.aircraft.map((a) =>
      `<span>${esc(a.flight || a.type)} ${esc(a.type)} · ${a.km} km</span>`).join("")}</div>
    <p class="hint">Marinspaning, tankflyg och transportflyg koncentreras där
    kriser byggs upp — ofta innan något rapporteras officiellt.</p>`;
}

function updateShadowSection(id) {
  const box = $("#shadow-section");
  if (!box) return;
  const s = shadowData?.per_chokepoint?.[id];
  if (!s) {
    box.innerHTML = '<p class="hint">För få tankers i zonen för flaggstatistik.</p>';
    return;
  }
  const flags = Object.entries(s.top_flags || {})
    .map(([iso, n]) => `<span>${esc(iso)} ${n}</span>`).join("");
  box.innerHTML = `
    <table>
      <tr><td>Tankers i zonen</td><td class="val">${s.tankers}</td></tr>
      ${s.watchlisted ? `<tr><td>⚑ På bevakningslistan</td><td class="val" style="color:var(--red)">${s.watchlisted}</td></tr>` : ""}
      <tr><td>Under högriskflagg</td><td class="val">${s.shadow_flagged}
        (${Math.round(s.shadow_share * 100)} %)
        ${s.elevated ? '<span class="r-tag tag-ongoing">FÖRHÖJD</span>' : ""}</td></tr>
      <tr><td>Bekvämlighetsflagg (FOC)</td><td class="val">${Math.round(s.foc_share * 100)} %</td></tr>
    </table>
    <div class="fact-kinds">${flags}</div>
    ${s.examples?.length ? `<div class="fc-why">${s.examples.map(esc).join(" · ")}</div>` : ""}
    ${s.note ? `<div class="drift-note">${esc(s.note)}</div>` : ""}`;
}

// ---------- Mörka fartyg (AIS-gap) + omlastningar till havs ----------
let darkData = null;
let darkEntities = [];

async function loadDark() {
  try {
    const d = await getJSON("/api/darkfleet");
    darkData = d;
    darkEntities.forEach((e) => viewer.entities.remove(e));
    darkEntities = [];
    for (const v of MAP_LAYERS.dark ? d.dark || [] : []) {
      const c = Cesium.Color.fromCssColorString(v.tanker ? "#ff2e63" : "#ff8c42");
      darkEntities.push(viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(v.lon, v.lat),
        point: { pixelSize: v.tanker ? 9 : 6, color: c.withAlpha(0.9),
                 outlineColor: c.withAlpha(0.25), outlineWidth: 8 },
        label: {
          text: `◌ ${v.name} (${v.gap_hours} h tyst)`,
          font: "600 11px Rajdhani, sans-serif",
          fillColor: Cesium.Color.fromCssColorString("#ffc0d0"),
          outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -15),
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 3.0e6),
        },
      }));
    }
    for (const t of MAP_LAYERS.dark ? d.sts || [] : []) {
      darkEntities.push(viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(t.lon, t.lat),
        point: { pixelSize: 10,
                 color: Cesium.Color.fromCssColorString("#4dc3ff").withAlpha(0.9),
                 outlineColor: Cesium.Color.fromCssColorString("#4dc3ff").withAlpha(0.25),
                 outlineWidth: 9 },
        label: {
          text: `⇄ ${t.a.name || t.a.mmsi} ⇄ ${t.b.name || t.b.mmsi}`,
          font: "600 11px Rajdhani, sans-serif",
          fillColor: Cesium.Color.fromCssColorString("#bfe6ff"),
          outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -15),
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 3.0e6),
        },
      }));
    }
    renderDarkPanel(d);
  } catch (e) { console.warn("darkfleet:", e); }
}

function renderDarkPanel(d) {
  const box = $("#dark-panel");
  if (!box) return;
  const rows = (d.dark || []).slice(0, 6).map((v) =>
    `<div class="rank-row"><span class="r-dot" style="background:${v.tanker ? "#ff2e63" : "#ff8c42"};box-shadow:0 0 7px ${v.tanker ? "#ff2e63" : "#ff8c42"}"></span>
     <span class="r-name">${esc(v.name)}${v.flag ? " · " + esc(v.flag) : ""}</span>
     <span class="r-score">${v.gap_hours} h</span></div>`).join("");
  const sts = (d.sts || []).slice(0, 4).map((t) =>
    `<div class="drift-note">⇄ ${esc(t.a.name || t.a.mmsi)} + ${esc(t.b.name || t.b.mmsi)}
     — ${t.distance_m} m i ${t.held_minutes} min${t.both_tankers ? " (båda tankers)" : ""}</div>`).join("");
  box.innerHTML = `
    <div class="mini"><b>${d.dark_total ?? 0}</b> mörka fartyg (varav
    <b>${d.dark_tankers ?? 0}</b> tankers) · <b>${d.sts_total ?? 0}</b> möjliga
    omlastningar · ${(d.tracked_vessels ?? 0).toLocaleString("sv-SE")} fartyg i registret</div>
    ${rows || '<p class="hint">Inga fartyg har tystnat i bevakade zoner.</p>'}
    ${sts}`;
}

// ---------- Satelliter i omloppsbana (Celestrak TLE) ----------
// Tredje transportskiktet över fartygen och flygplanen. SAR-satelliterna
// lyfts fram eftersom de är källan till vårt radarlager — och panelen visar
// när de nästa gång kan avbilda respektive sund.
const satPoints = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
let satData = null;
let satLabels = [];

async function loadSatellites() {
  try {
    const d = await getJSON("/api/satellites");
    satData = d;
    satPoints.removeAll();
    satLabels.forEach((e) => viewer.entities.remove(e));
    satLabels = [];
    for (const s of MAP_LAYERS.satellites ? d.items || [] : []) {
      const hl = s.highlight;
      satPoints.add({
        position: Cesium.Cartesian3.fromDegrees(s.lon, s.lat, s.alt_km * 1000),
        pixelSize: hl ? 8 : 2.6,
        color: hl
          ? Cesium.Color.fromCssColorString(
              s.role === "SAR-radar" ? "#ffe14d" : "#9fd8ff").withAlpha(0.98)
          : Cesium.Color.fromCssColorString("#7fa8c8").withAlpha(0.4),
        id: { satellite: s },
      });
      if (hl) {
        satLabels.push(viewer.entities.add({
          position: Cesium.Cartesian3.fromDegrees(s.lon, s.lat, s.alt_km * 1000),
          label: {
            text: "\u{1F6F0} " + s.name,
            font: "600 11px Rajdhani, sans-serif",
            fillColor: Cesium.Color.fromCssColorString(
              s.role === "SAR-radar" ? "#ffe14d" : "#cfe8ff"),
            outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            pixelOffset: new Cesium.Cartesian2(0, -14),
          },
        }));
      }
    }
    renderSatPanel(d);
    if (selectedId) updatePassSection(selectedId);
  } catch (e) { console.warn("satellites:", e); }
}

function renderSatPanel(d) {
  const box = $("#sat-panel");
  if (!box) return;
  const passes = Object.entries(d.next_passes || {})
    .sort((a, b) => a[1].in_minutes - b[1].in_minutes).slice(0, 5);
  const rows = passes.map(([cpId, p]) => {
    const cp = (overview?.items || []).find((i) => i.id === cpId);
    const h = Math.floor(p.in_minutes / 60), m = p.in_minutes % 60;
    return `<div class="rank-row" data-id="${esc(cpId)}">
      <span class="r-dot" style="background:#ffe14d;box-shadow:0 0 7px #ffe14d"></span>
      <span class="r-name">${esc(cp ? cp.name : cpId)} · ${esc(p.satellite)}</span>
      <span class="r-score">${h}h ${m}m</span></div>`;
  }).join("");
  const nSar = (d.items || []).filter((s) => s.role === "SAR-radar").length;
  box.innerHTML = `
    <div class="mini"><b>${(d.items || []).length}</b> satelliter i bana
    (<b>${nSar}</b> radarsatelliter) · nästa avbildningstillfälle per sund:</div>
    ${rows || '<p class="hint">Ingen passage inom 24 h.</p>'}`;
  document.querySelectorAll("#sat-panel .rank-row").forEach((r) =>
    r.addEventListener("click", () => selectChokepoint(r.dataset.id, true)));
}

function updatePassSection(id) {
  const box = $("#pass-section");
  if (!box) return;
  const p = satData?.next_passes?.[id];
  if (!p) { box.innerHTML = '<p class="hint">Ingen radarpassage inom 24 h.</p>'; return; }
  const h = Math.floor(p.in_minutes / 60), m = p.in_minutes % 60;
  box.innerHTML = `
    <table>
      <tr><td>Satellit</td><td class="val">${esc(p.satellite)}</td></tr>
      <tr><td>Nästa passage</td><td class="val">om ${h} h ${m} min</td></tr>
      <tr><td>Tidpunkt</td><td class="val">${esc(p.at)}</td></tr>
      <tr><td>Närmaste avstånd</td><td class="val">${p.distance_km} km</td></tr>
    </table>
    <p class="hint">Då kan sundet avbildas med radar — oberoende av om
    fartygen sänder AIS. Vårt SAR-lager bygger på just dessa passager.</p>`;
}

function renderSarPanel(d) {
  const box = $("#sar-panel");
  if (!box) return;
  const scenes = (d.scenes || []).map((sc) =>
    `<div class="rank-row"><span class="r-dot" style="background:#ffe14d;box-shadow:0 0 7px #ffe14d"></span>
     <span class="r-name">${esc(sc.aoi)} · ${esc(String(sc.scene_time).slice(0, 10))}</span>
     <span class="r-score">${sc.n}${sc.sized ? " / " + sc.sized + " m\u00e4tta" : ""}</span></div>`).join("");
  const sized = (d.detections || []).filter((x) => x.length_m)
    .sort((a, b) => b.length_m - a.length_m).slice(0, 6);
  const rows = sized.map((x) =>
    `<div class="drift-note">${x.length_m} m \u00b7 ${esc(x.class || "")} (${esc(x.aoi || "")})</div>`).join("");
  box.innerHTML = `
    <div class="mini"><b>${(d.detections || []).length}</b> radardetektioner,
    varav <b>${d.sized_total ?? 0}</b> storleksbest\u00e4mda med SAM</div>
    ${scenes}${rows}`;
}

// ---------- SAR-detektioner (Sentinel-1 — fartyg utan AIS) ----------
let sarEntities = [];
async function loadSar() {
  try {
    const d = await getJSON("/api/sar");
    sarEntities.forEach((e) => viewer.entities.remove(e));
    sarEntities = [];
    for (const det of MAP_LAYERS.sar ? d.detections || [] : []) {
      // Storleksbestämda detektioner (SAM) ritas större och bär längden —
      // ett 360 m skrov är en VLCC, ett 120 m är en kustare.
      const sized = !!det.length_m;
      sarEntities.push(viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(det.lon, det.lat),
        point: {
          pixelSize: sized ? 9 : 6,
          color: Cesium.Color.fromCssColorString(sized ? "#ffb454" : "#ffe14d")
            .withAlpha(0.95),
          outlineColor: Cesium.Color.fromCssColorString("#ffe14d").withAlpha(0.25),
          outlineWidth: sized ? 8 : 6,
        },
        label: sized ? {
          text: det.length_m + " m",
          font: "600 11px Rajdhani, sans-serif",
          fillColor: Cesium.Color.fromCssColorString("#ffe6b0"),
          outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -13),
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 2.0e6),
        } : undefined,
        properties: { sarNote: true },
      }));
    }
    renderSarPanel(d);
    if ((d.detections || []).length) {
      console.info(`SAR: ${d.detections.length} radardetektioner (scen ${d.scene_time || "?"})`);
    }
  } catch (e) { console.warn("sar:", e); }
}

// Live-lägesbild per sund (kön JUST NU — jämförs mot PortWatch-normen)
let liveStats = null;
async function loadLive() {
  try {
    liveStats = await getJSON("/api/live");
    if (overview) renderRanking(overview.items);
    if (selectedId) updateLiveSection(selectedId);
  } catch (e) { console.warn("live:", e); }
}

function liveFor(id) {
  return (liveStats && liveStats.per_chokepoint && liveStats.per_chokepoint[id]) || null;
}

function updateLiveSection(id) {
  const box = $("#live-section");
  const lv = liveFor(id);
  if (!box) return;
  if (!lv) { box.innerHTML = '<p class="hint">Live-AIS-data saknas för zonen just nu.</p>'; return; }
  const cats = Object.entries(lv.by_category || {})
    .map(([k, n]) => `<span>${esc(k)} ${n}</span>`).join("");
  const t = lv.trend_24h;
  const trendTxt = t
    ? `Trend: ${t.ships_now} fartyg nu vs ${t.ships_avg} i snitt senaste ${t.window_h} h; ` +
      `${t.anchored_now} ankrade vs ${t.anchored_avg}.`
    : "Trend byggs upp — egen live-historik spelas in var 5:e minut.";
  const hrs = (liveStats && liveStats.counter_hours) || 0;
  const tonnage = lv.est_tonnage_kt != null
    ? `<tr><td>Tonnage i zonen (skattat)</td><td class="val">~${lv.est_tonnage_kt.toLocaleString("sv-SE")} kt` +
      `<span class="dim" style="font-size:9.5px"> (${Math.round((lv.tonnage_coverage || 0) * 100)}% täckning)</span></td></tr>`
    : "";
  const entries = lv.entries_since_start != null && hrs >= 0.5
    ? `<tr><td>Zoninträden live (${hrs} h)</td><td class="val">${lv.entries_since_start}` +
      ` <span class="dim" style="font-size:9.5px">≈${Math.round(lv.entries_since_start / hrs * 24)}/dygn</span></td></tr>`
    : "";
  box.innerHTML = `
    <table>
      <tr><td>Fartyg i zonen (${lv.radius_km} km)</td><td class="val">${lv.ships}</td></tr>
      <tr><td>Varav ankrade/förtöjda ⚓</td><td class="val">${lv.anchored}</td></tr>
      <tr><td>Medianfart (i rörelse)</td><td class="val">${lv.median_sog_kn ?? "–"} kn</td></tr>
      ${tonnage}${entries}
      <tr><td>Norm (PortWatch, 7 år)</td><td class="val">~${lv.norm_transits_per_day ?? "–"} passager/dygn</td></tr>
    </table>
    <div class="fact-kinds">${cats}</div>
    <p class="hint" style="margin-top:5px">${esc(trendTxt)} Tonnage-proxy:
    L×B×djupgående×ρ×c_b (IMF WP/19/275). Terrester AIS är gles i
    Mellanöstern/Afrika — låga zontal där betyder täckning, inte tomt hav;
    PortWatch-satelliten ovan är sanningen om volymen.</p>`;
}

// ---------- Detaljpanel ----------
function fmtInt(n) {
  return n == null ? "–" : Math.round(n).toLocaleString("sv-SE");
}

function sparkline(canvas, spark, baseline) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const vals = spark.map((p) => p.value);
  const all = baseline != null ? vals.concat([baseline]) : vals;
  const min = Math.min(...all), max = Math.max(...all);
  const range = max - min || 1;
  const x = (i) => (i / (spark.length - 1)) * (w - 2) + 1;
  const y = (v) => h - 4 - ((v - min) / range) * (h - 10);

  if (baseline != null) {
    ctx.strokeStyle = "rgba(192,200,208,0.45)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(0, y(baseline)); ctx.lineTo(w, y(baseline));
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.strokeStyle = "#00ffc8";
  ctx.lineWidth = 1.6;
  ctx.shadowColor = "rgba(0,255,200,0.5)"; ctx.shadowBlur = 5;
  ctx.beginPath();
  spark.forEach((p, i) => { i ? ctx.lineTo(x(i), y(p.value)) : ctx.moveTo(x(i), y(p.value)); });
  ctx.stroke();
  ctx.shadowBlur = 0;
}

function horizonCell(entry) {
  if (!entry) return "<td>–</td>";
  const a = entry.analog, b = entry.base;
  if (a) {
    const edge = entry.edge_median;
    const ecls = edge > 0 ? "edge-pos" : edge < 0 ? "edge-neg" : "";
    // Leave-one-out riktningsträff: analogernas eget facit (events.py)
    const hit = a.tries ? `<br><span style="color:${a.hits / a.tries >= 0.6 ? "var(--green)" : "var(--dim)"};font-size:9px">✓ ${a.hits}/${a.tries} riktning</span>` : "";
    return `<td title="Analog: median ${a.median}%, band [${a.p10}%, ${a.p90}%], N=${a.n}` +
           (a.tries ? ` · LOO-riktningsträff ${a.hits}/${a.tries}` : "") +
           (b ? ` · Basnivå: ${b.median}%` : "") + `">` +
           `<span class="${ecls}">${a.median > 0 ? "+" : ""}${a.median}%</span>` +
           `<br><span style="color:var(--dim);font-size:9.5px">[${a.p10}, ${a.p90}]</span>${hit}</td>`;
  }
  if (b) {
    return `<td title="Basnivå: median ${b.median}%, band [${b.p10}%, ${b.p90}%], N=${b.n}">` +
           `${b.median > 0 ? "+" : ""}${b.median}%` +
           `<br><span style="color:var(--dim);font-size:9.5px">[${b.p10}, ${b.p90}]</span></td>`;
  }
  return "<td>–</td>";
}

let quotesCache = {};
async function ensureQuotes() {
  if (Object.keys(quotesCache).length) return quotesCache;
  try {
    const d = await getJSON("/api/markets");
    for (const q of d.quotes || []) quotesCache[q.symbol] = q;
  } catch (e) { /* tickern försöker igen */ }
  return quotesCache;
}

async function selectChokepoint(id, flyTo) {
  selectedId = id;
  $("#detail").classList.remove("hidden");
  $("#detail-content").innerHTML =
    '<div class="dim" style="padding:30px 0">Hämtar analys …<div class="loadbar"></div></div>';
  const [cp, quotes] = await Promise.all([
    getJSON("/api/chokepoint/" + encodeURIComponent(id)), ensureQuotes()]);
  if (cp.error) { $("#detail-content").innerHTML = `<p class="dim">${esc(cp.error)}</p>`; return; }

  if (flyTo) {
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(cp.lon, cp.lat - 8, 5200000),
      duration: 1.4,
    });
  }

  const s = cp.stress || {};
  const li = levelInfo(s.level);
  const fc = cp.forecast || {};
  const facts = cp.facts || {};

  const ongoing = s.ongoing_since
    ? `<div class="fc-note active"><b>Pågående störning</b> sedan ${esc(s.ongoing_since)}
       (${s.ongoing_days} dagar mot frusen förkris-baslinje).</div>` : "";

  const epRows = (cp.episodes || []).slice().reverse().map((ep) => {
    const label = episodeLabel(cp.id, ep) ||
      (cp.inverse ? "Trafiktopp (omdirigering)" : "Trafikfall");
    return `<div class="ep-row${ep.ongoing ? " ongoing" : ""}">
      <span class="e-date">${esc(ep.start)} → ${ep.ongoing ? "pågår" : esc(ep.end)}</span>
      <span class="e-label">${esc(label)}</span>
      <span class="e-z">z ${ep.peak_z}</span>
    </div>`;
  }).join("") || '<p class="hint">Inga episoder över tröskeln sedan 2019.</p>';

  const instRows = (fc.instruments || []).map((inst) => {
    const q = quotes[inst.symbol];
    const qtxt = q && q.price != null
      ? `${q.price} <span class="${q.change_pct > 0 ? "up" : q.change_pct < 0 ? "down" : ""}">` +
        `${q.change_pct > 0 ? "▲" : q.change_pct < 0 ? "▼" : ""}${q.change_pct ?? ""}%</span>` : "";
    const conf = inst.confidence === "svag"
      ? '<span class="conf-tag weak" title="Färre än 3 historiska episoder — banden är illustrativa">N&lt;3</span>'
      : "";
    const h = inst.horizons || {};
    return `<div class="fc-inst">
      <div class="fc-head"><span class="f-sym">${esc(inst.symbol)}</span>
        <span class="f-name">${esc(inst.name)}</span>${conf}
        <span class="f-quote">${qtxt}</span></div>
      <div class="fc-why">${esc(inst.why)}</div>
      <table class="fc-grid">
        <tr><th></th><th>+5 hd</th><th>+10 hd</th><th>+20 hd</th></tr>
        <tr><td>${fc.active ? "analog" : "basnivå"}</td>
          ${horizonCell(h["5"] || h[5])}${horizonCell(h["10"] || h[10])}${horizonCell(h["20"] || h[20])}</tr>
      </table>
    </div>`;
  }).join("");

  const kinds = (facts.by_kind || []).map((k) =>
    `<span>${esc(k.kind)} ${fmtInt(k.count)}/år</span>`).join("");

  $("#detail-content").innerHTML = `
    <h2>${esc(cp.name)}</h2>
    <div class="hint">${esc(cp.trade_note)}</div>
    ${cp.inverse ? '<div class="fc-note">Invers nod: <b>stigande</b> trafik här är stressignalen (mottagare av omdirigerad trafik).</div>' : ""}

    <div class="big-score ${li.cls}">${s.score ?? "–"}<small> /100 · ${esc(s.level || "okänt")}</small></div>
    <div class="gauge-wrap">
      <div class="gauge-bar"><div class="gauge-fill" style="width:${s.score || 0}%;background:${scoreColor(s.score)};box-shadow:0 0 9px ${scoreColor(s.score)}"></div></div>
      <div class="gauge-meta"><span>tonnage ${s.deviation_pct > 0 ? "+" : ""}${s.deviation_pct ?? "–"}% mot baslinje</span><span>riktad z ${s.directed_z ?? "–"}</span></div>
    </div>
    ${ongoing}

    <h4>Förseningsrisk — väder, larm, kö (48 h)</h4>
    <div id="delay-section"><p class="hint">Beräknar risk …</p></div>

    <h4>Drivande &amp; loitering — råvarusignal</h4>
    <div id="drift-section"><p class="hint">Läser driftdata …</p></div>

    <h4>Live just nu — zonen runt sundet</h4>
    <div id="live-section"><p class="hint">Läser live-AIS …</p></div>

    <h4>Flygfrakt i zonen (substitutionskanal)</h4>
    <div id="air-section"><p class="hint">Läser ADS-B …</p></div>

    <h4>Flaggstat &amp; skuggflotta (tankers)</h4>
    <div id="shadow-section"><p class="hint">Läser flaggstatistik …</p></div>

    <h4>Nästa radarpassage</h4>
    <div id="pass-section"><p class="hint">Beräknar bana …</p></div>

    <h4>Militärflyg i närområdet (≤400 km)</h4>
    <div id="mil-section"><p class="hint">Läser militärflyg …</p></div>

    <h4>Tonnage (DWT) — 120 dagar</h4>
    <canvas class="spark-canvas" id="spark"></canvas>
    <div class="spark-cap"><span>7d-medel: ${fmtInt(s.current_7d)} DWT</span><span>baslinje: ${fmtInt(s.baseline_median)}</span></div>
    <p class="hint" style="margin-top:4px">Senaste datapunkt: ${esc(s.last_date || "–")} (PortWatch släpar ~5 dygn). Streckad linje = ${s.ongoing_since ? "frusen förkris-baslinje" : "rullande baslinje"}.</p>

    <h4>Episoder sedan 2019 (${(cp.episodes || []).length})</h4>
    ${epRows}

    <h4>Prisscenarier — ${fc.active ? "analogläge" : "basnivå"}</h4>
    <div class="fc-note${fc.active ? " active" : ""}">${esc(fc.note || "")}</div>
    ${instRows}
    <p class="hint"><b>Läsanvisning:</b> siffran är medianutfall, [p10, p90] det empiriska
    bandet, "+5 hd" = 5 handelsdagar efter episodstart. Edge = analog − basnivå.
    <b>Ej finansiell rådgivning.</b></p>

    <h4>Om sundet (PortWatch)</h4>
    <table>
      <tr><td>Fartyg/år</td><td class="val">${fmtInt(facts.vessels_per_year)}</td></tr>
      <tr><td>Andel sjöburen olja</td><td class="val">${cp.oil_share_pct || 0}%</td></tr>
      <tr><td>Bevaka</td><td class="val" style="font-size:11px">${esc(cp.watch)}</td></tr>
    </table>
    <div class="fact-kinds">${kinds}</div>
  `;

  const spark = s.spark || [];
  if (spark.length > 1) sparkline($("#spark"), spark, s.baseline_median);
  updateLiveSection(cp.id);
  updateDelaySection(cp.id);
  updateAirSection(cp.id);
  updateShadowSection(cp.id);
  updateMilSection(cp.id);
  updatePassSection(cp.id);
  updateDriftSection(cp.id);
}

$("#detail-close").addEventListener("click", () => {
  $("#detail").classList.add("hidden");
  selectedId = null;
  if (scenarioActive) clearScenario();
});

// Tidsmaskin, scenario och brief
$("#replay-open").addEventListener("click", replayStart);
$("#replay-stop").addEventListener("click", () => replayStop(false));
$("#scenario-run").addEventListener("click", () => {
  const id = $("#scenario-select").value;
  if (id) runScenario(id);
});
$("#brief-open").addEventListener("click", showBrief);
$("#brief-close").addEventListener("click", () => $("#brief-overlay").classList.add("hidden"));
$("#brief-overlay").addEventListener("click", (e) => {
  if (e.target.id === "brief-overlay") $("#brief-overlay").classList.add("hidden");
});

// Klick på globen → välj chokepoint eller hamn
const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
handler.setInputAction((movement) => {
  const picked = viewer.scene.pick(movement.position);
  const cpId = picked?.id?.properties?.cpId?.getValue?.();
  if (cpId) { selectChokepoint(cpId, false); return; }
  const portId = picked?.id?.properties?.portId?.getValue?.();
  if (portId) { selectPort(portId); return; }
  const laneId = picked?.id?.properties?.laneId?.getValue?.();
  if (laneId) { selectLane(laneId); return; }
  if (picked?.id?.vessel) { selectVessel(picked.id.vessel); return; }
  if (picked?.id?.flight) { selectFlight(picked.id.flight); return; }
  if (picked?.id?.satellite) selectSatellite(picked.id.satellite);
}, Cesium.ScreenSpaceEventType.LEFT_CLICK);

function selectFlight(f) {
  selectedId = null;
  $("#detail").classList.remove("hidden");
  $("#detail-content").innerHTML = `
    <h2>${esc(f.flight)} ${f.cargo ? "✈ FRAKT" : ""}</h2>
    <div class="hint">${esc(f.type || "okänd typ")}${f.reg ? " · " + esc(f.reg) : ""}</div>
    <table style="margin-top:10px">
      <tr><td>Höjd</td><td class="val">${f.alt_ft ? f.alt_ft.toLocaleString("sv-SE") + " ft" : "–"}</td></tr>
      <tr><td>Fart / kurs</td><td class="val">${f.gs_kn ?? "–"} kn / ${f.track != null ? Math.round(f.track) + "°" : "–"}</td></tr>
      <tr><td>Position</td><td class="val">${f.lat.toFixed(3)}, ${f.lon.toFixed(3)}</td></tr>
      <tr><td>Nära</td><td class="val">${esc(f.near)}</td></tr>
      <tr><td>Källa</td><td class="val">ADS-B (adsb.lol, ODbL)</td></tr>
    </table>
    ${f.cargo ? '<div class="drift-note">Fraktflyg. Hög fraktandel över ett stört sund indikerar att högvärdesgods flyttar från sjö till luft.</div>' : ""}`;
}

function selectSatellite(s) {
  selectedId = null;
  $("#detail").classList.remove("hidden");
  $("#detail-content").innerHTML = `
    <h2>${esc(s.name)}</h2>
    <div class="hint">${esc(s.role || "satellit")}${s.why ? " · " + esc(s.why) : ""}</div>
    <table style="margin-top:10px">
      <tr><td>NORAD-id</td><td class="val">${s.norad}</td></tr>
      <tr><td>Position</td><td class="val">${s.lat.toFixed(2)}, ${s.lon.toFixed(2)}</td></tr>
      <tr><td>Höjd</td><td class="val">${Math.round(s.alt_km)} km</td></tr>
      <tr><td>Källa</td><td class="val">Celestrak GP (TLE)</td></tr>
    </table>`;
}

// ---------- Start ----------
getJSON("/api/config").then((cfg) => {
  if (cfg.cesium_ion_token) {
    Cesium.Ion.defaultAccessToken = cfg.cesium_ion_token;
    try { viewer.scene.setTerrain(Cesium.Terrain.fromWorldTerrain()); } catch (e) { /* valfritt */ }
  }
}).catch(console.warn);

loadOverview();
loadTicker();
loadVessels();
loadLive();
loadDelays();
loadDrift();
loadPorts();
loadSar();
loadFlights();
loadShadow();
loadDark();
loadSatellites();
setInterval(loadTicker, 120000);
setInterval(loadVessels, 60000);
setInterval(loadLive, 90000);
setInterval(loadDelays, 300000);
setInterval(loadDrift, 300000);
setInterval(loadPorts, 300000);
setInterval(loadFlights, 120000);
setInterval(loadShadow, 300000);
setInterval(loadDark, 300000);
setInterval(loadSatellites, 60000);
setInterval(() => { if (overview) loadOverview(); }, 600000);
