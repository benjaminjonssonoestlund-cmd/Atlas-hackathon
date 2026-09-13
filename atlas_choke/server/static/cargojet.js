/* ============================================================================
   CARGOJET ✈ — flottan på Atlas-globen, i två lägen:

   LIVE      planen i luften just nu (/api/cargojet/live) med dagens spår.
   SIMULERA  kvartalets flygningar (/api/cargojet/history?since&until) spelas
             upp i komprimerad tid; linjer växer fram bakom planen och
             försvinner vid landning. Dygn utan ADS-B-data hoppas över.

   Klick på ett plan väljer det (onSelect). Styrs av tracker.js via
   window.cargojetLayer. Kräver window.atlasGlobe (globe.js).
   ========================================================================== */

"use strict";

(() => {
  const viewer = window.atlasGlobe;
  if (!viewer) return;

  const LIVE_POLL_MS = 30000;
  const MIN_AIRCRAFT_PER_DAY = 5;       // färre plan = dygnet saknar data (samma regel som backend)
  const DAY = 86400;

  // ---------- Utseende (samma som tidigare programmet) ----------
  const PLANE_PATH =
    "M32 3c2.4 0 3.9 2.6 3.9 6.2v14.6l21.6 12.3v5.1l-21.6-6.6v14.2l7 5.2v4.3L32 55.6 " +
    "21.1 58.3V54l7-5.2V34.6L6.5 41.2v-5.1l21.6-12.3V9.2C28.1 5.6 29.6 3 32 3z";
  function planeIcon(fill, glow) {
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">` +
      `<defs><filter id="g" x="-40%" y="-40%" width="180%" height="180%">` +
      `<feGaussianBlur stdDeviation="2.6"/></filter></defs>` +
      `<path d="${PLANE_PATH}" fill="${glow}" opacity="0.85" filter="url(#g)"/>` +
      `<path d="${PLANE_PATH}" fill="${fill}" stroke="#140d02" stroke-width="1.3" stroke-linejoin="round"/>` +
      `</svg>`;
    return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  }
  const ICON = {
    live: planeIcon("#ffc53d", "#ff9d00"),
    replay: planeIcon("#fff1c9", "#ffb020"),
    ground: planeIcon("#8a95a0", "#3a4652"),
    selected: planeIcon("#7dfff0", "#00ffc8"),
  };
  const GOLD = Cesium.Color.fromCssColorString("#ffb020");
  const CYAN = Cesium.Color.fromCssColorString("#00ffc8");
  const PLANE_SCALE = new Cesium.NearFarScalar(2.0e5, 1.25, 1.2e7, 0.55);
  const MAT = {
    live: Cesium.Material.fromType("PolylineGlow", { color: GOLD.withAlpha(0.7), glowPower: 0.18 }),
    trail: Cesium.Material.fromType("PolylineGlow", { color: GOLD.withAlpha(0.8), glowPower: 0.2 }),
    selected: Cesium.Material.fromType("PolylineGlow", { color: CYAN.withAlpha(0.95), glowPower: 0.28 }),
  };

  const routes = viewer.scene.primitives.add(new Cesium.PolylineCollection());
  const planes = viewer.scene.primitives.add(new Cesium.BillboardCollection({ scene: viewer.scene }));
  const labels = viewer.scene.primitives.add(new Cesium.LabelCollection({ scene: viewer.scene }));
  const trails = new Map();             // flight-id → Polyline (simulering)
  const listeners = new Set();
  const selectListeners = new Set();

  const state = {
    mode: "live",
    live: [],
    flights: [], since: 0, until: 0, t: 0,
    speed: 21600, playing: true,
    coveredDays: [],                    // UTC-dygnsstarter med data, sorterade
    loading: false,
    request: 0,
    selected: null,                     // "live:<hex>" | "sim:<flight-id>"
  };

  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

  // ---------- Geometri ----------
  function bearing(lon1, lat1, lon2, lat2) {
    const r = Math.PI / 180;
    const y = Math.sin((lon2 - lon1) * r) * Math.cos(lat2 * r);
    const x = Math.cos(lat1 * r) * Math.sin(lat2 * r) -
      Math.sin(lat1 * r) * Math.cos(lat2 * r) * Math.cos((lon2 - lon1) * r);
    return (Math.atan2(y, x) / r + 360) % 360;
  }

  /** Position längs en flygnings spår vid tiden t: [lon, lat, alt_m, kurs]. */
  function positionAt(path, t) {
    let lo = 0, hi = path.length - 1;
    if (t <= path[0][3]) return [path[0][0], path[0][1], path[0][2], path[0][4] ?? 0];
    if (t >= path[hi][3]) return [path[hi][0], path[hi][1], path[hi][2], path[hi][4] ?? 0];
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (path[mid][3] <= t) lo = mid; else hi = mid;
    }
    const a = path[lo], b = path[hi];
    const f = (t - a[3]) / Math.max(1, b[3] - a[3]);
    const hdg = (a[0] === b[0] && a[1] === b[1]) ? (a[4] ?? 0) : bearing(a[0], a[1], b[0], b[1]);
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, hdg];
  }

  function pathPositions(path) {
    const flat = [];
    for (const p of path) flat.push(p[0], p[1], Math.max(p[2], 0));
    return Cesium.Cartesian3.fromDegreesArrayHeights(flat);
  }

  /** Spåret från avgång fram till planets nuvarande position `head`. */
  function trailPositions(path, t, head) {
    const flat = [];
    for (const p of path) {
      if (p[3] >= t) break;
      flat.push(p[0], p[1], Math.max(p[2], 0));
    }
    flat.push(head[0], head[1], Math.max(head[2], 0));
    return Cesium.Cartesian3.fromDegreesArrayHeights(flat);
  }

  function addPlane(pos, hdg, icon, size, id) {
    planes.add({
      position: pos, image: icon, id,
      width: size, height: size,
      rotation: -Cesium.Math.toRadians(hdg || 0),
      alignedAxis: Cesium.Cartesian3.UNIT_Z,
      scaleByDistance: PLANE_SCALE,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    });
  }

  function addLabel(pos, text, css) {
    labels.add({
      position: pos, text,
      font: "600 13px Rajdhani, sans-serif",
      fillColor: Cesium.Color.fromCssColorString(css),
      outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
      style: Cesium.LabelStyle.FILL_AND_OUTLINE,
      pixelOffset: new Cesium.Cartesian2(0, -22),
      distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 6.0e6),
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    });
  }

  function clearScene() {
    routes.removeAll();
    trails.clear();
    planes.removeAll();
    labels.removeAll();
  }

  // ---------- LIVE ----------
  function renderLive() {
    clearScene();
    for (const a of state.live) {
      const sel = `live:${a.hex}` === state.selected;
      const pos = Cesium.Cartesian3.fromDegrees(a.lon, a.lat, (a.alt_ft || 0) * 0.3048);
      if (!a.on_ground && a.path && a.path.length >= 2) {
        routes.add({ positions: pathPositions(a.path), width: sel ? 4 : 2.4, material: sel ? MAT.selected : MAT.live });
      }
      const icon = sel ? ICON.selected : a.on_ground ? ICON.ground : ICON.live;
      addPlane(pos, a.track, icon, sel ? 42 : a.on_ground ? 24 : 34, { kind: "live", item: a });
      if (!a.on_ground || sel) addLabel(pos, a.callsign, sel ? "#7dfff0" : "#ffd477");
    }
  }

  async function loadLive() {
    try {
      const d = await getJSON("/api/cargojet/live");
      state.live = d.items || [];
      if (state.mode === "live") renderLive();
      if (state.selected && state.selected.startsWith("live:")) {
        const item = state.live.find((a) => `live:${a.hex}` === state.selected);
        notifySelect(item ? { kind: "live", item } : null);
        if (!item) state.selected = null;
      }
      emit();
    } catch (e) {
      console.warn("cargojet live:", e);
    }
  }

  // ---------- SIMULERA ----------
  function renderSim() {
    planes.removeAll();
    labels.removeAll();
    const active = new Set();
    for (const f of state.flights) {
      if (state.t < f.t0 || state.t > f.t1) continue;
      const sel = `sim:${f.id}` === state.selected;
      const head = positionAt(f.path, state.t);
      const pos = Cesium.Cartesian3.fromDegrees(head[0], head[1], Math.max(head[2], 0));
      active.add(f.id);
      const positions = trailPositions(f.path, state.t, head);
      const material = sel ? MAT.selected : MAT.trail;
      const width = sel ? 4.5 : 2.2;
      const line = trails.get(f.id);
      if (line) {
        line.positions = positions;
        if (line.material !== material) line.material = material;
        if (line.width !== width) line.width = width;
      } else {
        trails.set(f.id, routes.add({ positions, width, material }));
      }
      addPlane(pos, head[3], sel ? ICON.selected : ICON.replay, sel ? 40 : 30, { kind: "sim", flight: f });
      addLabel(pos, f.callsign || f.reg, sel ? "#7dfff0" : "#ffe3a3");
    }
    for (const [id, line] of trails) {           // landade: linjen försvinner
      if (!active.has(id)) {
        routes.remove(line);
        trails.delete(id);
      }
    }
  }

  function coveredDayStarts(flights) {
    const craft = new Map();
    for (const f of flights) {
      for (let d = Math.floor(f.t0 / DAY) * DAY; d <= f.t1; d += DAY) {
        if (!craft.has(d)) craft.set(d, new Set());
        craft.get(d).add(f.hex);
      }
    }
    return [...craft].filter(([, s]) => s.size >= MIN_AIRCRAFT_PER_DAY).map(([d]) => d).sort((a, b) => a - b);
  }

  /** Hoppa över dygn utan data (t.ex. serverluckan på adsb.lol). */
  function skipGaps() {
    const days = state.coveredDays;
    if (!days.length) return;
    const day = Math.floor(state.t / DAY) * DAY;
    if (days.includes(day)) return;
    const next = days.find((d) => d > day);
    state.t = Math.max(state.since, next !== undefined ? next : days[0]);
  }

  // ---------- Val ----------
  function notifySelect(sel) {
    selectListeners.forEach((fn) => fn(sel));
  }

  function select(sel) {
    state.selected = sel ? (sel.kind === "live" ? `live:${sel.item.hex}` : `sim:${sel.flight.id}`) : null;
    if (state.mode === "live") renderLive();
    else if (state.flights.length) renderSim();
    notifySelect(sel);
  }

  new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas).setInputAction((m) => {
    const picked = viewer.scene.pick(m.position);
    const id = picked && picked.id;
    select(id && (id.kind === "live" || id.kind === "sim") ? id : null);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // ---------- Lägesbyte ----------
  async function setMode(mode, range) {
    const request = ++state.request;
    select(null);
    if (mode !== "sim") {
      state.mode = "live";
      state.loading = false;
      renderLive();
      emit();
      return;
    }
    state.mode = "sim";
    state.loading = true;
    state.flights = [];
    state.playing = true;
    clearScene();
    emit();
    try {
      const d = await getJSON(`/api/cargojet/history?since=${Math.floor(range.since)}&until=${Math.floor(range.until)}`);
      if (request !== state.request) return;       // användaren hann byta läge
      state.flights = (d.flights || []).filter((f) => f.path && f.path.length >= 2);
      state.since = d.since;
      state.until = d.until;
      state.coveredDays = coveredDayStarts(state.flights);
      state.t = state.since;
      skipGaps();
    } catch (e) {
      console.warn("cargojet simulering:", e);
    }
    if (request === state.request) {
      state.loading = false;
      emit();
    }
  }

  function info() {
    return {
      mode: state.mode,
      loading: state.loading,
      t: state.t,
      since: state.since,
      until: state.until,
      speed: state.speed,
      playing: state.playing,
      covered: state.coveredDays.length,
      airborne: state.mode === "sim"
        ? state.flights.filter((f) => state.t >= f.t0 && state.t <= f.t1).length
        : state.live.filter((a) => !a.on_ground).length,
    };
  }

  function emit() {
    const i = info();
    listeners.forEach((fn) => fn(i));
  }

  let lastFrame = performance.now();
  let lastDraw = 0;
  let lastEmit = 0;
  viewer.scene.preRender.addEventListener(() => {
    const now = performance.now();
    const dt = (now - lastFrame) / 1000;
    lastFrame = now;
    if (state.mode !== "sim" || !state.flights.length) return;
    if (state.playing) {
      state.t += dt * state.speed;
      if (state.t > state.until) state.t = state.since;   // loopa kvartalet
      skipGaps();
    }
    if (now - lastDraw >= 50) {
      lastDraw = now;
      renderSim();
    }
    if (now - lastEmit >= 250) {
      lastEmit = now;
      emit();
    }
  });

  window.cargojetLayer = {
    get mode() { return state.mode; },
    get playing() { return state.playing; },
    get speed() { return state.speed; },
    get range() { return { since: state.since, until: state.until }; },
    get coveredDays() { return state.coveredDays.slice(); },
    info,
    setMode,
    setPlaying(on) { state.playing = Boolean(on); emit(); },
    setSpeed(v) { if (v > 0) { state.speed = v; emit(); } },
    seek(t) {
      if (state.mode !== "sim" || !state.flights.length) return;
      state.t = Math.min(state.until, Math.max(state.since, t));
      renderSim();
      emit();
    },
    select,
    onSelect(fn) { selectListeners.add(fn); return () => selectListeners.delete(fn); },
    subscribe(fn) {
      listeners.add(fn);
      fn(info());
      return () => listeners.delete(fn);
    },
  };

  loadLive();
  setInterval(loadLive, LIVE_POLL_MS);
})();
