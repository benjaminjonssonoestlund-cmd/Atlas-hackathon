/* ============================================================================
   ATLAS-GLOBEN — helskärm, samma som tidigare programmet: Esri-satellit
   (dämpad), stjärnhimmel, atmosfär, tunna landsgränser och Cesium ion-token
   ur /api/config (ger världsterräng när den är satt).
   Exponeras som window.atlasGlobe (+ window.atlasGlobeHome) för övriga skript.
   ========================================================================== */

"use strict";

(() => {
  const ESRI_IMAGERY =
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
  // Cargojets nät: Hamilton-navet (YHM) och Nordamerika
  const HOME = Cesium.Cartesian3.fromDegrees(-88, 44, 8500000);

  Cesium.Ion.defaultAccessToken = undefined;

  const imagery = new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({
    url: ESRI_IMAGERY,
    credit: new Cesium.Credit("© Esri, Maxar, Earthstar Geographics"),
    maximumLevel: 19,
  }));
  // Ljusare än atlas-earths recon-preset: kontinenterna syns, planen lyser ändå.
  imagery.brightness = 0.62;
  imagery.saturation = 0.62;
  imagery.gamma = 0.92;

  const viewer = new Cesium.Viewer("globe", {
    baseLayer: imagery,
    baseLayerPicker: false, geocoder: false, timeline: false, animation: false,
    sceneModePicker: false, navigationHelpButton: false, homeButton: false,
    infoBox: false, selectionIndicator: false, fullscreenButton: false,
    creditContainer: document.getElementById("globe-credits"),
  });

  // Ingen dag/natt-skugga: hela jorden ska vara läsbar.
  viewer.scene.globe.enableLighting = false;
  viewer.scene.skyAtmosphere.show = true;
  viewer.camera.setView({ destination: HOME });

  // Landsgränser (Natural Earth) — Esri-satelliten saknar politiska linjer.
  const BORDER = Cesium.Color.fromCssColorString("#5f8f9c").withAlpha(0.42);
  Cesium.GeoJsonDataSource.load("/static/countries.geojson", {
    stroke: BORDER, fill: Cesium.Color.TRANSPARENT, strokeWidth: 1,
  }).then((ds) => {
    for (const e of ds.entities.values) {
      if (e.polygon) {
        e.polygon.fill = false;
        e.polygon.outline = true;
        e.polygon.outlineColor = BORDER;
      }
    }
    viewer.dataSources.add(ds);
  }).catch((e) => console.warn("gränser:", e));

  // Samma API-nyckel som tidigare programmet: Cesium ion-token från servern.
  fetch("/api/config").then((r) => r.json()).then((cfg) => {
    if (!cfg.cesium_ion_token) return;
    Cesium.Ion.defaultAccessToken = cfg.cesium_ion_token;
    try { viewer.scene.setTerrain(Cesium.Terrain.fromWorldTerrain()); } catch (e) { /* valfritt */ }
  }).catch((e) => console.warn("config:", e));

  // HUD-avläsning av kameran
  const readout = document.getElementById("hud-readout");
  if (readout) {
    viewer.camera.percentageChanged = 0.002;
    const update = () => {
      const c = viewer.camera.positionCartographic;
      const lat = Cesium.Math.toDegrees(c.latitude);
      const lon = Cesium.Math.toDegrees(c.longitude);
      readout.textContent =
        `CAM ${Math.abs(lat).toFixed(3)}°${lat >= 0 ? "N" : "S"}  ${Math.abs(lon).toFixed(3)}°${lon >= 0 ? "E" : "W"}` +
        `  ·  ALT ${Math.round(c.height / 1000).toLocaleString("en-US")} KM`;
    };
    viewer.camera.changed.addEventListener(update);
    update();
  }

  window.atlasGlobe = viewer;
  window.atlasGlobeHome = () => viewer.camera.flyTo({ destination: HOME, duration: 1.4 });
})();
