/* ============================================================================
   CARGOJET ✈ — historiska flygningar på globen.

   Data: /api/cargojet/* (adsb.lol globe_history per plan och dag + inspelade
   live-spår så att historiken når fram till nu). Flottan spelas upp i
   komprimerad tid: varje flygnings linje växer fram bakom planet medan det
   flyger och försvinner när det landat.
   Delar `viewer`, `getJSON`, `esc` och `$` med app.js (laddas efter den).
   ========================================================================== */

"use strict";

(() => {
  // ---------- Flygplansikon (SVG → billboard, pekar norrut, roteras efter kurs) ----------
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
    replay: planeIcon("#fff1c9", "#ffb020"),
    selected: planeIcon("#7dfff0", "#00ffc8"),
  };
  const GOLD = Cesium.Color.fromCssColorString("#ffb020");
  const CYAN = Cesium.Color.fromCssColorString("#00ffc8");
  const PLANE_SCALE = new Cesium.NearFarScalar(2.0e5, 1.25, 1.2e7, 0.55);
  // Delade material: en materialinstans per linje kvävde renderingen.
  const MAT = {
    trail: Cesium.Material.fromType("PolylineGlow", { color: GOLD.withAlpha(0.8), glowPower: 0.2 }),
    selected: Cesium.Material.fromType("PolylineGlow", { color: CYAN.withAlpha(0.95), glowPower: 0.28 }),
  };
  const ET = "America/Toronto";

  // ---------- Primitiver ----------
  const routes = viewer.scene.primitives.add(new Cesium.PolylineCollection());
  const planes = viewer.scene.primitives.add(new Cesium.BillboardCollection({ scene: viewer.scene }));
  const labels = viewer.scene.primitives.add(new Cesium.LabelCollection({ scene: viewer.scene }));

  const state = {
    flights: [],
    since: 0, until: 0,
    t: 0, playing: true, speed: 21600,
    selected: null,           // flight-id
    filter: "",
  };

  // ---------- Hjälpfunktioner ----------
  const fmtTime = (t, opts) => new Date(t * 1000).toLocaleString("sv-SE",
    { timeZone: ET, ...opts });
  const fmtClock = (t) => fmtTime(t, { weekday: "short", day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit" });
  const fmtDur = (m) => `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
  const apCode = (ap) => (ap ? ap.iata : "?");
  const apName = (ap) => (ap ? `${ap.iata} · ${ap.name}` : "okänt (täckningshål)");

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

  function matches(f) {
    if (!state.filter) return true;
    const q = state.filter;
    return [f.callsign, f.reg, f.type, f.dep?.iata, f.arr?.iata, f.dep?.name, f.arr?.name]
      .some((v) => (v || "").toLowerCase().includes(q));
  }

  function addPlane(pos, hdg, icon, id, size) {
    return planes.add({
      position: pos, image: icon, id,
      width: size, height: size,
      rotation: -Cesium.Math.toRadians(hdg || 0),
      alignedAxis: Cesium.Cartesian3.UNIT_Z,
      scaleByDistance: PLANE_SCALE,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    });
  }

  function addLabel(pos, text, color) {
    labels.add({
      position: pos, text,
      font: "600 12px Rajdhani, sans-serif",
      fillColor: color, outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
      style: Cesium.LabelStyle.FILL_AND_OUTLINE,
      pixelOffset: new Cesium.Cartesian2(0, -22),
      distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 6.0e6),
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    });
  }

  // ---------- Linjer ----------
  // Inga färdiga rutter: varje linje växer fram bakom planet medan det flyger
  // (renderFrame) och tas bort när det landat.
  const trails = new Map();   // flight-id → Polyline

  function clearTrails() {
    routes.removeAll();
    trails.clear();
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

  // ---------- Plan ----------
  function renderFrame() {
    planes.removeAll();
    labels.removeAll();
    const active = new Set();
    for (const f of state.flights) {
      if (state.t < f.t0 || state.t > f.t1 || !matches(f)) continue;
      const head = positionAt(f.path, state.t);
      const [lon, lat, alt, hdg] = head;
      const pos = Cesium.Cartesian3.fromDegrees(lon, lat, Math.max(alt, 0));
      const sel = f.id === state.selected;
      active.add(f.id);

      const material = sel ? MAT.selected : MAT.trail;
      const width = sel ? 4.5 : 2.2;
      const line = trails.get(f.id);
      if (!line) {
        trails.set(f.id, routes.add({ positions: trailPositions(f.path, state.t, head), width, material }));
      } else {
        line.positions = trailPositions(f.path, state.t, head);
        if (line.material !== material) line.material = material;
        if (line.width !== width) line.width = width;
      }

      addPlane(pos, hdg, sel ? ICON.selected : ICON.replay, { cjtFlight: f }, sel ? 40 : 30);
      addLabel(pos, f.callsign || f.reg, sel ? CYAN : Cesium.Color.fromCssColorString("#ffe3a3"));
    }
    // Landade (eller bortfiltrerade) flygningar: linjen försvinner
    for (const [id, line] of trails) {
      if (!active.has(id)) {
        routes.remove(line);
        trails.delete(id);
      }
    }
  }

  // ---------- Uppspelning ----------
  let lastFrame = performance.now();
  let lastDraw = 0;
  viewer.scene.preRender.addEventListener(() => {
    const now = performance.now();
    const dt = (now - lastFrame) / 1000;
    lastFrame = now;
    if (!state.flights.length) return;
    if (state.playing) {
      state.t += dt * state.speed;
      if (state.t > state.until) state.t = state.since;   // loopa
    }
    if (now - lastDraw < 50) return;                        // ~20 fps räcker
    lastDraw = now;
    renderFrame();
    updateClock();
  });

  function updateClock() {
    const span = Math.max(1, state.until - state.since);
    $("#cjt-slider").value = String(Math.round(((state.t - state.since) / span) * 1000));
    const airborne = state.flights.filter((f) => state.t >= f.t0 && state.t <= f.t1 && matches(f)).length;
    const day = Math.min(Math.ceil((state.t - state.since) / 86400) || 1, Math.ceil(span / 86400));
    $("#cjt-clock").innerHTML =
      `<b>${esc(fmtClock(state.t))}</b> <span class="dim">Toronto · dygn ${day}/${Math.ceil(span / 86400)}</span>` +
      `<span class="cjt-air">✈ ${airborne} i luften</span>`;
  }

  // ---------- Lista ----------
  function renderList() {
    const list = state.flights.filter(matches);
    const hours = list.reduce((s, f) => s + f.dur_min, 0) / 60;
    const tails = new Set(list.map((f) => f.hex)).size;
    $("#cjt-summary").innerHTML = `<b>${list.length}</b> flygningar · <b>${Math.round(hours)}</b> flygtimmar · ${tails} plan`;
    $("#cjt-list").innerHTML = list.map((f) => `
      <div class="cjt-row ${f.id === state.selected ? "sel" : ""}" data-id="${esc(f.id)}">
        <span class="cjt-cs">${esc(f.callsign || f.reg)}</span>
        <span class="cjt-route">${esc(apCode(f.dep))} → ${esc(apCode(f.arr))}</span>
        <span class="cjt-meta">${esc(fmtTime(f.t0, { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" }))}</span>
      </div>`).join("") ||
      '<p class="hint">Inga flygningar i intervallet än — historiken hämtas i bakgrunden (se status nedan).</p>';
  }

  $("#cjt-list").addEventListener("click", (e) => {
    const row = e.target.closest(".cjt-row");
    if (row?.dataset.id) selectFlight(state.flights.find((f) => f.id === row.dataset.id), true);
  });

  // ---------- Detaljpanel ----------
  function flyToPath(path) {
    if (!path || path.length < 2) return;
    const sphere = Cesium.BoundingSphere.fromPoints(pathPositions(path));
    viewer.camera.flyToBoundingSphere(sphere, {
      duration: 1.4,
      offset: new Cesium.HeadingPitchRange(0, -Cesium.Math.PI_OVER_TWO, Math.max(sphere.radius * 3.2, 9e5)),
    });
  }

  function selectFlight(f, fly) {
    if (!f) return;
    state.selected = f.id;
    state.t = f.t0;
    clearTrails(); renderList();
    if (fly) flyToPath(f.path);
    $("#detail").classList.remove("hidden");
    $("#detail-content").innerHTML = `
      <div class="cjt-card-head">
        <img src="${ICON.selected}" alt="" class="cjt-card-icon">
        <div><h2>${esc(f.callsign || f.reg)}</h2>
        <div class="hint">Cargojet · ${esc(f.type || "okänd typ")} · ${esc(f.reg)}</div></div>
      </div>
      <div class="cjt-leg">
        <div><div class="cjt-ap">${esc(apCode(f.dep))}</div><div class="hint">${esc(fmtTime(f.t0, { hour: "2-digit", minute: "2-digit" }))}</div></div>
        <div class="cjt-leg-line"><span>✈</span><small>${fmtDur(f.dur_min)}</small></div>
        <div><div class="cjt-ap">${esc(apCode(f.arr))}</div><div class="hint">${esc(fmtTime(f.t1, { hour: "2-digit", minute: "2-digit" }))}</div></div>
      </div>
      <table>
        <tr><td>Från</td><td class="val">${esc(apName(f.dep))}</td></tr>
        <tr><td>Till</td><td class="val">${esc(apName(f.arr))}</td></tr>
        <tr><td>Datum (Toronto)</td><td class="val">${esc(fmtTime(f.t0, { dateStyle: "medium" }))}</td></tr>
        <tr><td>Luftburen tid</td><td class="val">${fmtDur(f.dur_min)}</td></tr>
        <tr><td>Max höjd</td><td class="val">${f.max_alt_ft.toLocaleString("sv-SE")} ft</td></tr>
        <tr><td>Mode S-hex</td><td class="val">${esc(f.hex)}</td></tr>
        <tr><td>Källa</td><td class="val">adsb.lol globe_history (ODbL)</td></tr>
      </table>
      <p class="hint">Tiden är luftburen tid ur ADS-B, inte blocktid. "?" = spåret började eller
      slutade i ett täckningshål. <a href="https://globe.adsb.lol/?icao=${esc(f.hex)}&showTrace=${esc(fmtTime(f.t0, { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit" }))}" target="_blank" rel="noopener">Öppna i adsb.lol ↗</a></p>`;
  }

  new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas).setInputAction((m) => {
    const picked = viewer.scene.pick(m.position);
    if (picked?.id?.cjtFlight) selectFlight(picked.id.cjtFlight, false);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // ---------- Data ----------
  async function loadHistory() {
    const v = $("#cjt-range").value;
    const q = v.startsWith("day:") ? `day=${v.slice(4)}` : `days=${Number(v)}`;
    if (!state.flights.length) $("#cjt-summary").textContent = "Laddar flygningar …";
    try {
      const d = await getJSON(`/api/cargojet/history?${q}`);
      state.flights = (d.flights || []).filter((f) => f.path && f.path.length >= 2);
      state.since = d.since; state.until = d.until;
      // Simuleringen spelar hela intervallet från början (30 dygn bakåt som standard)
      if (!state.t || state.t < state.since || state.t > state.until) state.t = state.since;
      clearTrails(); renderList(); updateClock();
    } catch (e) { console.warn("cargojet history:", e); }
  }

  async function loadStatus() {
    try {
      const s = await getJSON("/api/cargojet/status");
      const b = s.backfill || {};
      const days = (s.archive || []).length;
      fillArchive(s.archive || []);
      const busy = b.current_day
        ? ` · hämtar ${b.current_day} (${b.fetched} anrop hittills)`
        : "";
      $("#cjt-status").textContent =
        `Arkiv: ${days} dygn med spår (mål ${s.history_days} dygn bakåt)${busy}. ` +
        `Källa: adsb.lol globe_history, ODbL 1.0.`;
    } catch (e) { /* status är bara information */ }
  }

  // Arkivdygn (egen backfill + cjt_nowcast-dumpar) som val i intervallväljaren
  function fillArchive(archive) {
    const sel = $("#cjt-range");
    let group = sel.querySelector("optgroup");
    if (!group) {
      group = document.createElement("optgroup");
      group.label = "Arkiv — enskilt dygn (UTC)";
      sel.appendChild(group);
    }
    const have = new Set([...group.children].map((o) => o.value));
    for (const a of archive) {
      const value = "day:" + a.day;
      if (have.has(value)) continue;
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = `${a.day} · ${a.aircraft} plan`;
      group.appendChild(opt);
    }
    [...group.children]
      .sort((x, y) => y.value.localeCompare(x.value))
      .forEach((o) => group.appendChild(o));
  }

  // ---------- Kontroller ----------
  $("#cjt-play").addEventListener("click", () => {
    state.playing = !state.playing;
    $("#cjt-play").textContent = state.playing ? "❚❚" : "▶";
  });
  $("#cjt-slider").addEventListener("input", (e) => {
    state.t = state.since + (Number(e.target.value) / 1000) * (state.until - state.since);
    renderFrame(); updateClock();
  });
  $("#cjt-speed").addEventListener("change", (e) => { state.speed = Number(e.target.value); });
  $("#cjt-range").addEventListener("change", () => { state.t = 0; loadHistory(); });
  $("#cjt-search").addEventListener("input", (e) => {
    state.filter = e.target.value.trim().toLowerCase();
    clearTrails(); renderList();
  });

  loadHistory();
  loadStatus();
  setInterval(loadStatus, 20000);
  setInterval(loadHistory, 300000);
})();
