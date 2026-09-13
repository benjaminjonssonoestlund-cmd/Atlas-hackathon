/* ============================================================================
   CARGOJET TRACKER — HUD-panelerna runt globen.

   Allt hämtas från backend:
     /api/trackers                    modulväljaren
     /api/tracker/<id>/history        backtestens kvartal (konsensus, signal, utfall, rätt/fel)
     /api/tracker/<id>/current        flygtimmar hittills mot samma period 1–2 år bakåt
   Globen styrs via window.cargojetLayer (LIVE / SIMULERA, uppspelning, val av plan).
   ========================================================================== */

"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ET = "America/Toronto";
  const DAY = 86400;
  const CURRENT_POLL_MS = 60000;
  const HISTORY_POLL_MS = 600000;
  const SPEEDS = [{ v: 3600, label: "1H/S" }, { v: 21600, label: "6H/S" }, { v: 86400, label: "1D/S" }];
  const FILTERS = ["ALL", "CORRECT", "WRONG", "HIGHER", "LOWER"];
  const AUTOSTART_SPEED = 86400;        // simuleringen startar med 1 dygn per sekund

  const state = {
    trackers: [], trackerId: null,
    history: null, current: null,
    filter: "ALL", openCard: null, openCompare: null,
    shownHours: 0, hoursAnim: 0,
    timers: [],
    simRangeKey: "",
    lastTicker: "",
  };
  const layer = () => window.cargojetLayer;

  // ---------- Format ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtCad = (m) => (Number.isFinite(m) ? `${m.toFixed(1)}M CAD` : "–");
  const fmtHours = (h) => (Number.isFinite(h) ? Math.round(h).toLocaleString("en-US") : "–");
  const fmtPct = (x) => (Number.isFinite(x) ? `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}%` : "–");
  const parts = (fmt, t) => Object.fromEntries(fmt.formatToParts(new Date(t * 1000)).map((p) => [p.type, p.value]));
  const DT_ET = new Intl.DateTimeFormat("en-GB", {
    timeZone: ET, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const simClock = (t) => { const p = parts(DT_ET, t); return `${p.day} ${p.month} ${p.hour}:${p.minute}`.toUpperCase(); };
  const TIME_ET = new Intl.DateTimeFormat("en-GB", { timeZone: ET, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  const TIME_UTC = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  const DAY_ET = new Intl.DateTimeFormat("en-GB", { timeZone: ET, day: "2-digit", month: "short" });
  const HM_ET = new Intl.DateTimeFormat("en-GB", { timeZone: ET, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

  // ---------- Klocka ----------
  function tickClock() {
    const now = new Date();
    $("clock").innerHTML = `<span>UTC <b>${TIME_UTC.format(now)}</b></span><span>YHM <b>${TIME_ET.format(now)}</b></span>`;
  }
  tickClock();
  setInterval(tickClock, 1000);

  // ---------- Topplistans nyckeltal ----------
  function renderTicker(liveAirborne) {
    const q = [];
    const h = state.history, c = state.current;
    if (h && h.total) q.push(`<span class="q">BACKTEST <b>${h.hits}/${h.total}</b> RÄTT</span>`);
    if (c && Number.isFinite(c.hours)) {
      q.push(`<span class="q">${esc(c.quarter)} ${esc(c.year)} <b>${fmtHours(c.hours)} H</b></span>`);
      const prev = (c.comparisons || [])[0];
      if (prev && Number.isFinite(prev.hours) && prev.hours > 0) {
        const yoy = c.hours / prev.hours - 1;
        q.push(`<span class="q">YOY <b class="${yoy >= 0 ? "up" : "down"}">${fmtPct(yoy)}</b> VS ${esc(prev.year)}</span>`);
      }
      if (Number.isFinite(c.coverage)) q.push(`<span class="q">TÄCKNING <b>${Math.round(c.coverage * 100)}%</b></span>`);
    }
    if (Number.isFinite(liveAirborne)) q.push(`<span class="q">✈ <b>${liveAirborne}</b> I LUFTEN</span>`);
    const html = q.join("") || '<span class="dim">Hämtar trackerdata …</span>';
    if (html !== state.lastTicker) {
      $("ticker").innerHTML = html;
      state.lastTicker = html;
    }
  }

  // ---------- Modulväljare ----------
  function renderMenu() {
    const active = state.trackers.find((t) => t.id === state.trackerId);
    $("tracker-name").textContent = active ? active.name : "–";
    $("tracker-menu").innerHTML = state.trackers.map((t) => `
      <button class="tracker-option${t.id === state.trackerId ? " is-active" : ""}" type="button"
              role="menuitem" data-id="${esc(t.id)}">${esc(t.name)}<small>${esc(t.ticker || "")}</small></button>`).join("");
  }

  function setMenuOpen(open) {
    $("tracker-menu").hidden = !open;
    $("tracker-select").setAttribute("aria-expanded", open ? "true" : "false");
  }

  $("tracker-select").addEventListener("click", (e) => {
    e.stopPropagation();
    setMenuOpen($("tracker-menu").hidden);
  });
  $("tracker-menu").addEventListener("click", (e) => {
    const opt = e.target.closest(".tracker-option");
    if (opt) selectTracker(opt.dataset.id);
    setMenuOpen(false);
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#tracker-menu")) setMenuOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    setMenuOpen(false);
    layer()?.select(null);
  });

  // ---------- HISTORY ----------
  function matchesFilter(it) {
    switch (state.filter) {
      case "CORRECT": return it.correct;
      case "WRONG": return !it.correct;
      case "HIGHER": return it.signal === "HIGHER";
      case "LOWER": return it.signal === "LOWER";
      default: return true;
    }
  }

  function renderSummary() {
    const h = state.history;
    const items = h.items || [];
    const pct = h.total ? h.hits / h.total : 0;
    const C = 2 * Math.PI * 24;
    const chrono = [...items].reverse();
    $("history-summary").innerHTML = items.length ? `
      <div class="acc">
        <div class="acc-ring-wrap">
          <svg class="acc-ring" viewBox="0 0 58 58" aria-hidden="true">
            <circle class="ring-bg" cx="29" cy="29" r="24"/>
            <circle class="ring-fg" cx="29" cy="29" r="24" stroke-dasharray="${(C * pct).toFixed(1)} ${C.toFixed(1)}"/>
          </svg>
          <div class="acc-pct">${Math.round(pct * 100)}<small>%</small></div>
        </div>
        <div class="acc-text">
          <b>${h.hits}/${h.total}</b>
          <span>DIRECTION HITS</span>
          <em>${esc(h.metric || "REVENUE")} VS KONSENSUS</em>
        </div>
      </div>
      <div class="acc-strip">${chrono.map((it) => `
        <button class="acc-cell ${it.correct ? "hit" : "miss"}${it.id === state.openCard ? " is-open" : ""}"
                type="button" data-id="${esc(it.id)}" aria-label="${esc(`${it.quarter} ${it.year}`)}"></button>`).join("")}
      </div>
      <div class="acc-strip-labels"><span>${esc(`${chrono[0].quarter} ${chrono[0].year}`)}</span>
        <span>${esc(`${chrono[chrono.length - 1].quarter} ${chrono[chrono.length - 1].year}`)}</span></div>` : "";

    $("history-filters").innerHTML = FILTERS.map((f) => {
      const n = f === "ALL" ? items.length : items.filter((it) => {
        const prev = state.filter; state.filter = f;
        const ok = matchesFilter(it); state.filter = prev; return ok;
      }).length;
      return `<button class="chip${f === state.filter ? " is-active" : ""}" type="button" data-filter="${f}">${f}<small>${n}</small></button>`;
    }).join("");
  }

  function QuarterCard(it, index) {
    const open = it.id === state.openCard;
    const up = it.signal === "HIGHER";
    const cons = it.consensus_mcad, fc = it.forecast_mcad, act = it.result_mcad;
    const surprise = act / cons - 1;
    const fcErr = fc / act - 1;
    const lo = Math.min(cons, fc, act), hi = Math.max(cons, fc, act);
    const pad = (hi - lo) * 0.2 || 2;
    const pos = (v) => (((v - (lo - pad)) / ((hi + pad) - (lo - pad))) * 100).toFixed(1);
    return `
      <article class="q-card ${it.correct ? "is-hit" : "is-miss"}${open ? " is-open" : ""}" data-id="${esc(it.id)}"
               role="button" tabindex="0" aria-expanded="${open}" style="animation-delay:${index * 35}ms">
        <div class="q-head">
          <span class="q-name"><b>${esc(it.quarter)}</b> ${esc(it.year)}</span>
          <span class="q-badge">${it.correct ? "CORRECT" : "WRONG"}</span>
        </div>
        <div class="q-row"><span>KONSENSUS</span><b>${fmtCad(cons)}</b></div>
        <div class="q-row"><span>OUR TRACKER</span><b class="${up ? "sig-up" : "sig-down"}">${up ? "▲" : "▼"} ${esc(it.signal)}</b></div>
        <div class="q-row"><span>RESULT</span><b>${fmtCad(act)}</b></div>
        <div class="q-detail"><div><div class="q-detail-inner">
          <div class="scale" aria-hidden="true">
            <div class="scale-track"></div>
            <span class="mk mk-cons" style="left:${pos(cons)}%"><i></i><em>KONS</em></span>
            <span class="mk mk-fc" style="left:${pos(fc)}%"><i></i><em>FCST</em></span>
            <span class="mk mk-act" style="left:${pos(act)}%"><i></i><em>ACT</em></span>
          </div>
          <div class="q-row"><span>OUR FORECAST</span><b>${fmtCad(fc)}</b></div>
          <div class="q-row"><span>RESULT VS KONSENSUS</span><b class="${surprise >= 0 ? "sig-up" : "sig-down"}">${fmtPct(surprise)}</b></div>
          <div class="q-row"><span>FORECAST ERROR</span><b>${fmtPct(fcErr)}</b></div>
          <div class="q-row"><span>REPORT DATE</span><b>${esc(it.report_date || "–")}</b></div>
        </div></div></div>
      </article>`;
  }

  function renderCards() {
    const items = (state.history?.items || []).filter(matchesFilter);
    $("history-list").innerHTML = items.map(QuarterCard).join("") ||
      `<p class="list-note">${esc(state.history?.error || "Inga kvartal matchar filtret.")}</p>`;
  }

  function renderHistory() {
    if (!state.history) return;
    renderSummary();
    renderCards();
  }

  function toggleCard(id, scroll) {
    state.openCard = state.openCard === id ? null : id;
    const it = (state.history?.items || []).find((x) => x.id === id);
    if (it && !matchesFilter(it)) state.filter = "ALL";
    renderHistory();
    if (scroll && state.openCard) {
      document.querySelector(`.q-card[data-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  $("history-list").addEventListener("click", (e) => {
    const card = e.target.closest(".q-card");
    if (card) toggleCard(card.dataset.id, false);
  });
  $("history-list").addEventListener("keydown", (e) => {
    const card = e.target.closest(".q-card");
    if (card && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      toggleCard(card.dataset.id, false);
    }
  });
  $("history-summary").addEventListener("click", (e) => {
    const cell = e.target.closest(".acc-cell");
    if (cell) toggleCard(cell.dataset.id, true);
  });
  $("history-filters").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    state.filter = chip.dataset.filter;
    renderHistory();
  });
  $("history-collapse").addEventListener("click", () => $("history-panel").classList.toggle("is-collapsed"));

  // ---------- Kvartalspanelen ----------
  function quarterDays(startTs) {
    const p = parts(new Intl.DateTimeFormat("en-CA", { timeZone: ET, year: "numeric", month: "2-digit" }), startTs);
    const y = Number(p.year), m = Number(p.month);
    return Math.round((Date.UTC(y, m + 2, 1) - Date.UTC(y, m - 1, 1)) / 86400000);
  }

  function compareRows(c) {
    return [{ ...c, isCurrent: true }, ...(c.comparisons || [])];
  }

  function renderSheet() {
    const c = state.current;
    if (!c) return;
    const rows = compareRows(c);
    const maxH = Math.max(1, ...rows.map((r) => (Number.isFinite(r.hours) ? r.hours : 0)));
    const prev = (c.comparisons || [])[0];
    const yoy = Number.isFinite(c.hours) && prev && Number.isFinite(prev.hours) && prev.hours > 0 ? c.hours / prev.hours - 1 : NaN;
    const qDays = quarterDays(c.start_ts);
    const qPct = Math.min(100, Number.isFinite(c.elapsed_fraction) ? c.elapsed_fraction * 100 : (c.days / qDays) * 100);
    const total = Number.isFinite(c.hours) ? c.hours : 0;
    const hasSplit = Number.isFinite(c.measured_hours) && Number.isFinite(c.estimated_hours) && total > 0;
    const mPct = hasSplit ? (c.measured_hours / total) * 100 : 100;
    const ePct = hasSplit ? (c.estimated_hours / total) * 100 : 0;
    const levelCls = c.level === "HIGH" ? "is-high" : c.level === "LOW" ? "is-low" : "is-none";
    const sim = layer()?.mode === "sim";

    const sheet = $("sheet");
    sheet.classList.toggle("is-sim", sim);
    sheet.innerHTML = `
      <header class="sheet-head">
        <button id="sheet-collapse" class="collapse-btn" type="button" aria-label="Fäll ihop">▾</button>
        <div class="sheet-id">
          <span class="sheet-title">${esc(c.quarter)} ${esc(c.year)}</span>
          <span class="sheet-sub">QUARTER TO DATE · DAY ${c.days}/${qDays}</span>
        </div>
        <div class="qprog" aria-hidden="true"><i style="width:${qPct.toFixed(1)}%"></i></div>
        <button id="simulate" class="simulate${sim ? " is-active" : ""}" type="button" aria-pressed="${sim}">SIMULERA</button>
      </header>
      <div class="sheet-body">
        <div class="hours-block">
          <div>
            <div class="hours-line"><span id="hours-num" class="hours-num">${fmtHours(state.shownHours)}</span><span class="hours-unit">HOURS</span></div>
            <div class="hours-meta">${esc(c.unit || "")}${Number.isFinite(c.pace_quarter_hours)
              ? ` · PACE ${fmtHours(c.pace_quarter_hours)} H FULL QUARTER` : ""}</div>
          </div>
          <div class="level-block">
            <span class="level ${levelCls}">${esc(c.level || "–")}</span>
            <span class="yoy">YOY <b class="${yoy >= 0 ? "sig-up" : "sig-down"}">${fmtPct(yoy)}</b>${prev ? ` VS ${esc(prev.year)}` : ""}</span>
            ${c.level_basis ? `<span class="basis">${esc(c.level_basis)}</span>` : ""}
          </div>
        </div>

        <div>
          <div class="stack" aria-hidden="true"><i class="seg-m" style="width:${mPct.toFixed(1)}%"></i><i class="seg-e" style="width:${ePct.toFixed(1)}%"></i></div>
          <div class="coverage-meta">
            <span><i class="sw seg-m"></i>MEASURED ${fmtHours(c.measured_hours)} H</span>
            <span><i class="sw seg-e"></i>ESTIMATED ${fmtHours(c.estimated_hours)} H</span>
            ${Number.isFinite(c.covered_days) ? `<span>${c.covered_days}/${c.days} DAYS WITH DATA</span>` : ""}
            ${c.calibration && Number.isFinite(c.calibration.factor) ? `<span>CALIBRATION ×${c.calibration.factor.toFixed(2)}</span>` : ""}
          </div>
        </div>

        <div class="compare">${rows.map((r) => {
          const h = Number.isFinite(r.hours) ? r.hours : 0;
          const split = Number.isFinite(r.measured_hours) && Number.isFinite(r.estimated_hours);
          const m = ((split ? r.measured_hours : h) / maxH) * 100;
          const e = split ? (r.estimated_hours / maxH) * 100 : 0;
          const delta = !r.isCurrent && Number.isFinite(c.hours) && Number.isFinite(r.hours) && r.hours > 0 ? c.hours / r.hours - 1 : NaN;
          const open = state.openCompare === r.year;
          return `
            <div class="compare-row${r.isCurrent ? " is-current" : ""}${open ? " is-open" : ""}" data-year="${esc(r.year)}"
                 role="button" tabindex="0" aria-expanded="${open}">
              <span class="c-label"><b>${esc(r.quarter)}</b> ${esc(r.year)}</span>
              <span class="bar"><i class="seg-m" style="width:${m.toFixed(1)}%"></i><i class="seg-e" style="width:${e.toFixed(1)}%"></i></span>
              <span class="c-hours">${fmtHours(r.hours)} <small>H</small></span>
              <span class="c-delta ${r.isCurrent ? "now" : delta >= 0 ? "sig-up" : "sig-down"}">${r.isCurrent ? "NOW" : fmtPct(delta)}</span>
            </div>
            ${open ? `<div class="compare-detail">${compareDetail(r)}</div>` : ""}`;
        }).join("")}</div>

        <div class="sim-controls">
          <div class="sim-top">
            <button id="sim-play" class="chip sim-play" type="button" aria-label="Spela/pausa">❚❚</button>
            ${SPEEDS.map((s) => `<button class="chip sim-speed" type="button" data-speed="${s.v}">${s.label}</button>`).join("")}
            <span id="sim-readout" class="sim-readout"></span>
          </div>
          <div id="sim-timeline" class="timeline" role="slider" aria-label="Tidslinje">
            <div id="sim-days" class="timeline-days"></div>
            <div id="sim-playhead" class="playhead"></div>
          </div>
          <div id="sim-labels" class="timeline-labels"></div>
        </div>

        <p class="sheet-note">${esc(c.method || c.level_basis || "")}</p>
      </div>`;

    $("simulate").addEventListener("click", toggleSimulation);
    $("sheet-collapse").addEventListener("click", () => sheet.classList.toggle("is-collapsed"));
    sheet.querySelector(".compare").addEventListener("click", (e) => {
      const row = e.target.closest(".compare-row");
      if (!row) return;
      state.openCompare = state.openCompare === row.dataset.year ? null : row.dataset.year;
      renderSheet();
    });
    sheet.querySelector(".compare").addEventListener("keydown", (e) => {
      const row = e.target.closest(".compare-row");
      if (row && (e.key === "Enter" || e.key === " ")) {
        e.preventDefault();
        row.click();
      }
    });
    $("sim-play").addEventListener("click", () => layer()?.setPlaying(!layer().playing));
    sheet.querySelectorAll(".sim-speed").forEach((b) =>
      b.addEventListener("click", () => layer()?.setSpeed(Number(b.dataset.speed))));
    bindTimeline($("sim-timeline"));

    state.simRangeKey = "";
    animateHours(c.hours);
    if (layer()) onLayerChange(layer().info());
  }

  /** Detaljrader för en jämförelserad — visar bara fält som backend faktiskt levererar. */
  function compareDetail(r) {
    const lines = [];
    if (r.source) lines.push(`SOURCE <b>${esc(r.source)}</b>`);
    if (Number.isFinite(r.reported_quarter_hours)) {
      lines.push(`FULL QUARTER (REPORTED) <b>${fmtHours(r.reported_quarter_hours)} H</b>`);
    }
    if (r.isCurrent && Number.isFinite(r.pace_quarter_hours)) {
      lines.push(`FULL QUARTER PACE <b>${fmtHours(r.pace_quarter_hours)} H</b>`);
    }
    if (r.start_ts && r.end_ts) {
      lines.push(`WINDOW <b>${esc(DAY_ET.format(new Date(r.start_ts * 1000)))}</b> → <b>${esc(DAY_ET.format(new Date(r.end_ts * 1000)))} ${esc(HM_ET.format(new Date(r.end_ts * 1000)))}</b> ET`);
    }
    if (Number.isFinite(r.measured_hours) && Number.isFinite(r.estimated_hours)) {
      lines.push(`MEASURED <b>${fmtHours(r.measured_hours)} H</b> · ESTIMATED <b>${fmtHours(r.estimated_hours)} H</b>`);
    }
    if (Number.isFinite(r.covered_days) && Number.isFinite(r.days)) {
      lines.push(`COVERAGE <b>${r.covered_days}/${r.days}</b> DAYS (<b>${Math.round((r.coverage || 0) * 100)}%</b>)`);
    }
    if (r.isCurrent && r.calibration && Number.isFinite(r.calibration.factor)) {
      lines.push(`ADS-B → BLOCK HOURS CALIBRATION <b>×${r.calibration.factor.toFixed(2)}</b>`);
    }
    return lines.join("<br>") || "–";
  }

  function animateHours(target) {
    if (!Number.isFinite(target)) {
      state.shownHours = NaN;
      if ($("hours-num")) $("hours-num").textContent = "–";
      return;
    }
    const from = Number.isFinite(state.shownHours) ? state.shownHours : 0;
    const t0 = performance.now();
    const id = ++state.hoursAnim;
    const step = (now) => {
      if (id !== state.hoursAnim) return;
      const p = Math.min(1, (now - t0) / 1100);
      state.shownHours = from + (target - from) * (1 - Math.pow(1 - p, 3));
      const el = $("hours-num");
      if (el) el.textContent = fmtHours(state.shownHours);
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ---------- Simulering ----------
  function toggleSimulation() {
    const l = layer();
    if (!l || !state.current) return;
    if (l.mode === "sim") l.setMode("live");
    else l.setMode("sim", { since: state.current.start_ts, until: Date.now() / 1000 });
  }

  function bindTimeline(el) {
    let dragging = false;
    const seek = (e) => {
      const l = layer();
      const r = l?.range;
      if (!l || !r || !r.until) return;
      const box = el.getBoundingClientRect();
      const f = Math.min(1, Math.max(0, (e.clientX - box.left) / box.width));
      l.seek(r.since + f * (r.until - r.since));
    };
    el.addEventListener("pointerdown", (e) => {
      dragging = true;
      el.setPointerCapture?.(e.pointerId);
      seek(e);
    });
    el.addEventListener("pointermove", (e) => { if (dragging) seek(e); });
    const stop = () => { dragging = false; };
    el.addEventListener("pointerup", stop);
    el.addEventListener("pointercancel", stop);
  }

  function renderTimelineDays(info) {
    const l = layer();
    const key = `${info.since}-${info.until}-${info.covered}`;
    if (!l || key === state.simRangeKey || !$("sim-days")) return;
    state.simRangeKey = key;
    const covered = new Set(l.coveredDays);
    const first = Math.floor(info.since / DAY) * DAY;
    const cells = [];
    for (let d = first; d < info.until; d += DAY) {
      const isMonth = new Date(d * 1000).getUTCDate() === 1;
      cells.push(`<i class="${covered.has(d) ? "has-data" : ""}${isMonth ? " is-month" : ""}"></i>`);
    }
    $("sim-days").innerHTML = cells.join("");
    $("sim-labels").innerHTML = info.until
      ? `<span>${esc(DAY_ET.format(new Date(info.since * 1000)).toUpperCase())}</span><span>${covered.size} DAYS WITH DATA</span><span>${esc(DAY_ET.format(new Date(info.until * 1000)).toUpperCase())}</span>`
      : "";
  }

  function onLayerChange(info) {
    const sim = info.mode === "sim";
    $("live-dot").classList.toggle("is-sim", sim);
    $("mode-badge").classList.toggle("is-sim", sim);
    $("live-label").textContent = !sim ? "LIVE" : info.loading ? "LOADING" : `SIM ${simClock(info.t)}`;
    $("live-count").textContent = `✈ ${info.airborne}`;
    const sheet = $("sheet");
    sheet.classList.toggle("is-sim", sim);
    const btn = $("simulate");
    if (btn) {
      btn.classList.toggle("is-active", sim);
      btn.setAttribute("aria-pressed", sim ? "true" : "false");
    }
    if (sim && $("sim-readout")) {
      $("sim-readout").innerHTML = info.loading
        ? "LADDAR KVARTALETS FLYGNINGAR …"
        : `${esc(simClock(info.t))} ET · <b>✈ ${info.airborne}</b> AIRBORNE`;
      $("sim-play").textContent = info.playing ? "❚❚" : "▶";
      document.querySelectorAll(".sim-speed").forEach((b) =>
        b.classList.toggle("is-active", Number(b.dataset.speed) === info.speed));
      if (!info.loading && info.until) {
        renderTimelineDays(info);
        const f = (info.t - info.since) / Math.max(1, info.until - info.since);
        $("sim-playhead").style.left = `${(Math.min(1, Math.max(0, f)) * 100).toFixed(2)}%`;
      }
    }
    if (!sim) renderTicker(info.airborne);
  }

  // ---------- Flygkort (klick på ett plan) ----------
  function renderFlightCard(sel) {
    const card = $("flight-card");
    if (!sel) {
      card.hidden = true;
      return;
    }
    const row = (k, v) => `<div class="q-row"><span>${k}</span><b>${v}</b></div>`;
    let body;
    if (sel.kind === "live") {
      const a = sel.item;
      body = `
        ${row("STATUS", a.on_ground ? "ON GROUND" : "AIRBORNE")}
        ${a.dep ? row("DEPARTED", esc(`${a.dep.iata} · ${a.dep.name}`)) : ""}
        ${a.t0 ? row("AIRBORNE SINCE", esc(HM_ET.format(new Date(a.t0 * 1000))) + " ET") : ""}
        ${row("ALTITUDE", a.alt_ft ? `${a.alt_ft.toLocaleString("en-US")} FT` : "–")}
        ${row("SPEED / TRACK", `${a.gs_kn != null ? Math.round(a.gs_kn) : "–"} KN / ${a.track != null ? Math.round(a.track) + "°" : "–"}`)}
        ${row("MODE S", esc(a.hex))}`;
      card.innerHTML = cardShell(a.callsign, `${a.type || ""} · ${a.reg || ""}`, "LIVE", body);
    } else {
      const f = sel.flight;
      body = `
        <div class="fc-leg">
          <span class="fc-ap">${esc(f.dep ? f.dep.iata : "?")}</span>
          <span class="fc-line"><span>✈</span></span>
          <span class="fc-ap">${esc(f.arr ? f.arr.iata : "?")}</span>
        </div>
        <div class="fc-rows">
          ${row("DEPARTURE", esc(`${DAY_ET.format(new Date(f.t0 * 1000))} ${HM_ET.format(new Date(f.t0 * 1000))}`) + " ET")}
          ${row("ARRIVAL", esc(`${DAY_ET.format(new Date(f.t1 * 1000))} ${HM_ET.format(new Date(f.t1 * 1000))}`) + " ET")}
          ${row("AIRBORNE", `${Math.floor(f.dur_min / 60)} H ${String(f.dur_min % 60).padStart(2, "0")} MIN`)}
          ${row("MAX ALT", `${(f.max_alt_ft || 0).toLocaleString("en-US")} FT`)}
          ${row("MODE S", esc(f.hex))}
        </div>`;
      card.innerHTML = cardShell(f.callsign || f.reg, `${f.type || ""} · ${f.reg || ""}`, "SIM", body);
    }
    card.hidden = false;
    card.querySelector(".collapse-btn").addEventListener("click", () => layer()?.select(null));
  }

  function cardShell(title, sub, tag, body) {
    return `
      <header class="panel-head">
        <h3>✈ ${esc(title)} <span class="head-tag">${esc(tag)}</span></h3>
        <button class="collapse-btn" type="button" aria-label="Stäng">✕</button>
      </header>
      <div class="fc-body">
        <div class="q-row"><span>CARGOJET</span><b>${esc(sub)}</b></div>
        ${body}
      </div>`;
  }

  // ---------- Data ----------
  async function loadHistory() {
    try {
      state.history = await getJSON(`/api/tracker/${encodeURIComponent(state.trackerId)}/history`);
    } catch (e) {
      state.history = { items: [], error: e.message };
    }
    renderHistory();
    renderTicker();
  }

  async function loadCurrent() {
    try {
      const d = await getJSON(`/api/tracker/${encodeURIComponent(state.trackerId)}/current`);
      if (d.error) throw new Error(d.error);
      state.current = d;
      renderSheet();
      renderTicker();
      maybeAutostart();
    } catch (e) {
      console.warn("tracker current:", e);
      if (!state.current) setTimeout(loadCurrent, 10000);   // beräkningen kan pågå vid kallstart
    }
  }

  function selectTracker(id) {
    if (!id) return;
    state.trackerId = id;
    state.current = null;
    state.history = null;
    state.openCard = null;
    state.openCompare = null;
    renderMenu();
    state.timers.forEach(clearInterval);
    loadHistory();
    loadCurrent();
    state.timers = [setInterval(loadCurrent, CURRENT_POLL_MS), setInterval(loadHistory, HISTORY_POLL_MS)];
  }

  // ---------- Topplistans knappar ----------
  $("home-view").addEventListener("click", () => window.atlasGlobeHome?.());
  $("fullscreen").addEventListener("click", () => {
    const root = document.documentElement;
    const current = document.fullscreenElement || document.webkitFullscreenElement;
    if (current) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    else (root.requestFullscreen || root.webkitRequestFullscreen).call(root);
  });
  const onFullscreen = () => {
    const on = Boolean(document.fullscreenElement || document.webkitFullscreenElement);
    $("fullscreen").setAttribute("aria-pressed", on ? "true" : "false");
  };
  document.addEventListener("fullscreenchange", onFullscreen);
  document.addEventListener("webkitfullscreenchange", onFullscreen);

  // ---------- Autostart ----------
  // Direkt efter laddningsskärmen startar kvartalets simulering i 1D/S — en gång,
  // så att ett senare klick på SIMULERA (tillbaka till LIVE) respekteras.
  let autostarted = false;
  let bootDone = !window.atlasBoot || Boolean(window.atlasBootDone);
  function maybeAutostart() {
    const l = layer();
    if (autostarted || !bootDone || !l || !state.current) return;
    autostarted = true;
    l.setSpeed(AUTOSTART_SPEED);
    if (l.mode !== "sim") l.setMode("sim", { since: state.current.start_ts, until: Date.now() / 1000 });
  }
  window.addEventListener("atlas:boot-done", () => {
    bootDone = true;
    maybeAutostart();
  });

  // ---------- Start ----------
  if (layer()) {
    layer().subscribe(onLayerChange);
    layer().onSelect(renderFlightCard);
  }

  getJSON("/api/trackers").then((d) => {
    state.trackers = d.trackers || [];
    selectTracker(d.default || state.trackers[0]?.id);
  }).catch((e) => {
    $("history-list").innerHTML = `<p class="list-note">${esc(e.message)}</p>`;
  });
})();
