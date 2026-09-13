/* OPERATION CARGOJET — omsättning ÖVER/UNDER konsensus.
   Hämtar /api/all, ritar tre SVG-diagram och räknar om Q3-prognosen i webbläsaren
   med modellens egna koefficienter när reglaget flyttas (samma formel som
   model.forecast_quarter). All text från API:t sätts med textContent. */
"use strict";

(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const css = getComputedStyle(document.documentElement);
  const C = {
    model: css.getPropertyValue("--series-model").trim(),
    cons: css.getPropertyValue("--series-cons").trim(),
    actual: css.getPropertyValue("--series-actual").trim(),
    daily: css.getPropertyValue("--series-daily").trim(),
    surface: css.getPropertyValue("--surface-1").trim(),
    good: css.getPropertyValue("--good").trim(),
    critical: css.getPropertyValue("--critical").trim(),
  };
  const $ = (id) => document.getElementById(id);
  const nf = (v, d = 1) => v == null ? "–" : v.toLocaleString("sv-SE", { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (v, d = 1) => v == null ? "–" : `${v >= 0 ? "+" : "−"}${nf(Math.abs(v) * 100, d)} %`;
  const el = (tag, attrs = {}, parent) => {
    const n = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };
  const txt = (parent, x, y, s, attrs = {}) => { const t = el("text", { x, y, ...attrs }, parent); t.textContent = s; return t; };
  /** SVG i verklig pixelbredd: containerns bredd, men aldrig smalare än minW (då scrollar rutan). */
  const sizedSvg = (box, minW, H) => {
    const W = Math.max(Math.floor(box.clientWidth || minW), minW);
    return [el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` }, box), W];
  };
  /** Var n:te etikett så att etiketter om ~labelPx inte krockar i band om bandPx. */
  const labelStep = (bandPx, labelPx) => Math.max(1, Math.ceil(labelPx / bandPx));

  // ---------- Tooltip ----------
  const tip = $("tooltip");
  function showTip(evt, head, rows) {
    tip.replaceChildren();
    const h = document.createElement("div"); h.className = "tt-head"; h.textContent = head; tip.appendChild(h);
    for (const r of rows) {
      const row = document.createElement("div"); row.className = "tt-row";
      const k = document.createElement("span"); k.className = "k";
      if (r.color) {
        const key = document.createElementNS(SVG_NS, "svg"); key.setAttribute("width", "12"); key.setAttribute("height", "4");
        el("line", { x1: 0, y1: 2, x2: 12, y2: 2, stroke: r.color, "stroke-width": 2, "stroke-linecap": "round" }, key);
        k.appendChild(key);
      }
      k.appendChild(document.createTextNode(r.label));
      const v = document.createElement("span"); v.className = "v"; v.textContent = r.value;
      if (r.cls) v.classList.add(r.cls);
      row.append(k, v); tip.appendChild(row);
    }
    tip.hidden = false; moveTip(evt);
  }
  function moveTip(evt) {
    const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    let x = (evt.clientX ?? 0) + pad, y = (evt.clientY ?? 0) + pad;
    if (x + w > innerWidth - 8) x = (evt.clientX ?? 0) - w - pad;
    if (y + h > innerHeight - 8) y = (evt.clientY ?? 0) - h - pad;
    tip.style.left = `${Math.max(8, x)}px`; tip.style.top = `${Math.max(8, y)}px`;
  }
  const hideTip = () => { tip.hidden = true; };

  function legend(container, items) {
    container.replaceChildren();
    for (const it of items) {
      const s = document.createElement("span");
      const key = document.createElementNS(SVG_NS, "svg"); key.setAttribute("width", "16"); key.setAttribute("height", "10");
      if (it.shape === "dot") el("circle", { cx: 8, cy: 5, r: 4, fill: it.color }, key);
      else el("line", { x1: 1, y1: 5, x2: 15, y2: 5, stroke: it.color, "stroke-width": 2, "stroke-linecap": "round" }, key);
      s.appendChild(key); s.appendChild(document.createTextNode(it.label)); container.appendChild(s);
    }
  }

  function niceTicks(min, max, count = 5) {
    const span = max - min, raw = span / count, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || raw;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step, out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(6));
    return out;
  }

  const verdictNode = (container, dir, big = false) => {
    container.replaceChildren();
    const icon = document.createElement("span"); icon.className = "icon"; icon.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    if (dir === "ÖVER") { icon.textContent = "▲"; container.className = container.className.replace(/status-\w+/g, "") + " status-good"; label.textContent = "ÖVER konsensus"; }
    else if (dir === "UNDER") { icon.textContent = "▼"; container.className = container.className.replace(/status-\w+/g, "") + " status-critical"; label.textContent = "UNDER konsensus"; }
    else { icon.textContent = "■"; label.textContent = "I linje med konsensus"; }
    if (big) label.textContent = dir || "—";
    container.append(icon, label);
  };

  // ---------- Modellen i webbläsaren ----------
  let NC = null;
  const revenue = (h) => {
    const p = NC.params;
    return p.C * (1 + p.b0 + p.bh * h + p.bf * p.fuel_yoy + p.bx * p.fx_yoy) + p.K * p.jet * p.H * (1 + h) + p.A;
  };

  // ---------- 01 Signal ----------
  function renderSignal(tests, nc) {
    const dir = nc.forecast_total_rev > nc.consensus_revenue ? "ÖVER" : "UNDER";
    const diff = nc.forecast_total_rev - nc.consensus_revenue;
    verdictNode($("hero-verdict"), dir, true);
    $("hero-delta").textContent = `${diff >= 0 ? "+" : "−"}${nf(Math.abs(diff))} mkr (${pct(diff / nc.consensus_revenue)}) mot konsensus`;
    const noise = Math.abs(diff / nc.consensus_revenue) < 0.03;
    $("hero-caveat").textContent =
      `Bygger på ${nc.n_pairs ?? "?"} veckodagsmatchade ADS-B-dagar. ` +
      (noise ? "Skillnaden är mindre än mätosäkerheten i blocktimmarna — svag signal. " : "") +
      (nc.consensus_is_forward ? "Konsensus är forward per 2026-09-11, inte dagen före rapport." : "");
    $("t-model").textContent = nf(nc.forecast_total_rev);
    $("t-cons").textContent = nf(nc.consensus_revenue);
    $("t-cons-sub").textContent = `mkr CAD · rapport ${nc.report_date ?? "–"}`;
    $("t-hours").textContent = pct(nc.measured_hours_yoy);
    $("t-hours-sub").textContent = `segmentviktat · totalt ${pct(nc.measured_hours_yoy_total)}`;
    $("t-hits").textContent = `${tests.hits}/${tests.scored}`;
    $("t-hits-sub").textContent = `rätt riktning ${tests.first}–${tests.last} (${nf(100 * tests.hits / tests.scored, 0)} %)`;
  }

  // ---------- 02 Simulering ----------
  const SIM = { min: -0.2, max: 0.2 };
  function renderSimChart(h) {
    const box = $("sim-chart"); box.replaceChildren();
    const H = 300, m = { l: 56, r: 18, t: 22, b: 36 };
    const [svg, W] = sizedSvg(box, 460, H);
    const xs = (v) => m.l + (v - SIM.min) / (SIM.max - SIM.min) * (W - m.l - m.r);
    const ys0 = [revenue(SIM.min), revenue(SIM.max), NC.consensus_revenue];
    const ticks = niceTicks(Math.min(...ys0) * 0.99, Math.max(...ys0) * 1.01, 5);
    const yMin = ticks[0], yMax = ticks[ticks.length - 1];
    const ys = (v) => H - m.b - (v - yMin) / (yMax - yMin) * (H - m.t - m.b);
    const grid = el("g", { class: "grid" }, svg), axis = el("g", { class: "axis" }, svg);
    for (const t of ticks) {
      el("line", { x1: m.l, x2: W - m.r, y1: ys(t), y2: ys(t) }, grid);
      txt(axis, m.l - 8, ys(t) + 4, nf(t, 0), { "text-anchor": "end" });
    }
    for (const t of [-0.2, -0.1, 0, 0.1, 0.2]) txt(axis, xs(t), H - m.b + 18, pct(t, 0), { "text-anchor": "middle" });
    txt(axis, (m.l + W - m.r) / 2, H - 4, "blocktimmar Q3 2026 mot Q3 2025", { "text-anchor": "middle" });
    // konsensus (horisontell) och modell (linje)
    el("line", { x1: m.l, x2: W - m.r, y1: ys(NC.consensus_revenue), y2: ys(NC.consensus_revenue), stroke: C.cons, "stroke-width": 2 }, svg);
    el("line", { x1: xs(SIM.min), y1: ys(revenue(SIM.min)), x2: xs(SIM.max), y2: ys(revenue(SIM.max)), stroke: C.model, "stroke-width": 2, "stroke-linecap": "round" }, svg);
    // referenslinjer: uppmätt och brytpunkt
    const refs = [];
    if (NC.measured_hours_yoy != null) refs.push({ v: NC.measured_hours_yoy, label: "ADS-B" });
    if (NC.breakeven_hours_yoy != null && NC.breakeven_hours_yoy > SIM.min && NC.breakeven_hours_yoy < SIM.max) refs.push({ v: NC.breakeven_hours_yoy, label: "brytpunkt" });
    // Två närliggande referenslinjer: den vänstra får etiketten till vänster, den högra till höger
    refs.sort((a, b) => a.v - b.v).forEach((r, i) => {
      el("line", { x1: xs(r.v), x2: xs(r.v), y1: m.t, y2: H - m.b, stroke: "#4a515c", "stroke-width": 1 }, svg);
      const left = refs.length > 1 && i === 0;
      txt(axis, xs(r.v) + (left ? -5 : 5), m.t - 6, r.label, { "text-anchor": left ? "end" : "start" });
    });
    // aktuell punkt
    el("circle", { cx: xs(h), cy: ys(revenue(h)), r: 6, fill: C.model, stroke: C.surface, "stroke-width": 2 }, svg);
    // crosshair-hover längs x
    const hit = el("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" }, svg);
    const cross = el("line", { y1: m.t, y2: H - m.b, stroke: "#6d7178", "stroke-width": 1, visibility: "hidden" }, svg);
    hit.addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect();
      const vx = (e.clientX - r.left) / r.width * W;
      const hv = Math.max(SIM.min, Math.min(SIM.max, SIM.min + (vx - m.l) / (W - m.l - m.r) * (SIM.max - SIM.min)));
      const hr = Math.round(hv * 1000) / 1000;
      cross.setAttribute("x1", xs(hr)); cross.setAttribute("x2", xs(hr)); cross.setAttribute("visibility", "visible");
      const rev = revenue(hr);
      showTip(e, `blocktimmar ${pct(hr)}`, [
        { label: "Prognos", value: `${nf(rev)} mkr`, color: C.model },
        { label: "Konsensus", value: `${nf(NC.consensus_revenue)} mkr`, color: C.cons },
        { label: "Mot konsensus", value: pct(rev / NC.consensus_revenue - 1), cls: rev >= NC.consensus_revenue ? "status-good" : "status-critical" },
      ]);
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  }

  function updateSim(h) {
    $("hours-slider").value = (h * 100).toFixed(1);
    $("hours-out").textContent = pct(h);
    const rev = revenue(h), diff = rev - NC.consensus_revenue;
    $("r-hours").textContent = nf(NC.prev_block_hours * (1 + h), 0);
    $("r-rev").textContent = `${nf(rev)} mkr`;
    $("r-diff").textContent = `${diff >= 0 ? "+" : "−"}${nf(Math.abs(diff))} mkr (${pct(diff / NC.consensus_revenue)})`;
    $("r-break").textContent = NC.breakeven_hours_yoy == null ? "–" : pct(NC.breakeven_hours_yoy);
    const v = $("sim-verdict"); v.className = "verdict";
    verdictNode(v, diff > 0 ? "ÖVER" : diff < 0 ? "UNDER" : "LIKA");
    renderSimChart(h);
  }

  function initSim(nc) {
    legend($("sim-legend"), [{ label: "Modellens prognos", color: C.model }, { label: "Konsensus", color: C.cons }]);
    const slider = $("hours-slider");
    slider.addEventListener("input", () => updateSim(+slider.value / 100));
    document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
      const p = b.dataset.preset;
      const v = p === "measured" ? nc.measured_hours_yoy : p === "total" ? nc.measured_hours_yoy_total : nc.breakeven_hours_yoy;
      if (v != null) updateSim(Math.max(SIM.min, Math.min(SIM.max, v)));
    }));
    updateSim(nc.measured_hours_yoy ?? 0);
    // kontroll: webbläsarens formel ska ge exakt serverns prognos
    if (nc.measured_hours_yoy != null && Math.abs(revenue(nc.measured_hours_yoy) - nc.forecast_total_rev) > 0.05) {
      console.warn("Simuleringsformeln avviker från serverns prognos", revenue(nc.measured_hours_yoy), nc.forecast_total_rev);
    }
  }

  // ---------- 03 Historik ----------
  function renderHistory(tests) {
    const rows = tests.rows;
    legend($("hist-legend"), [
      { label: "Prognos", color: C.model, shape: "dot" },
      { label: "Konsensus", color: C.cons },
      { label: "Utfall", color: C.actual, shape: "dot" },
    ]);
    const box = $("hist-chart"); box.replaceChildren();
    const H = 340, m = { l: 56, r: 16, t: 18, b: 62 };
    const [svg, W] = sizedSvg(box, 640, H);
    const vals = rows.flatMap((r) => [r.prognos, r.konsensus, r.utfall]).filter((v) => v != null);
    const ticks = niceTicks(Math.min(...vals) * 0.97, Math.max(...vals) * 1.02, 5);
    const yMin = ticks[0], yMax = ticks[ticks.length - 1];
    const ys = (v) => H - m.b - (v - yMin) / (yMax - yMin) * (H - m.t - m.b);
    const band = (W - m.l - m.r) / rows.length;
    const grid = el("g", { class: "grid" }, svg), axis = el("g", { class: "axis" }, svg);
    for (const t of ticks) {
      el("line", { x1: m.l, x2: W - m.r, y1: ys(t), y2: ys(t) }, grid);
      txt(axis, m.l - 8, ys(t) + 4, nf(t, 0), { "text-anchor": "end" });
    }
    rows.forEach((r, i) => {
      const x0 = m.l + i * band, cx = x0 + band / 2, isNow = r.timmar !== "rapporterade (MD&A)";
      if (isNow) {
        el("rect", { x: x0 + 2, y: m.t, width: band - 4, height: H - m.t - m.b, class: "nowcast-band" }, svg);
        txt(axis, cx, m.t + 10, "NOWCAST", { "text-anchor": "middle" });
      }
      const hover = el("rect", { x: x0 + 2, y: m.t, width: band - 4, height: H - m.t - m.b, class: "band-hover" }, svg);
      if (r.konsensus != null) el("line", { x1: cx - 16, x2: cx + 16, y1: ys(r.konsensus), y2: ys(r.konsensus), stroke: C.cons, "stroke-width": 2, "stroke-linecap": "round" }, svg);
      if (r.prognos != null) el("circle", { cx: cx - 7, cy: ys(r.prognos), r: 5, fill: C.model, stroke: C.surface, "stroke-width": 2 }, svg);
      if (r.utfall != null) el("circle", { cx: cx + 7, cy: ys(r.utfall), r: 5, fill: C.actual, stroke: C.surface, "stroke-width": 2 }, svg);
      txt(axis, cx, H - m.b + 18, r.kvartal, { "text-anchor": "middle" });
      const mark = r.rätt == null ? "–" : r.rätt ? "✓ rätt" : "✗ fel";
      const mt = txt(axis, cx, H - m.b + 36, mark, { "text-anchor": "middle" });
      if (r.rätt != null) mt.setAttribute("style", `fill:${r.rätt ? C.good : C.critical}`);
      const hit = el("rect", { x: x0, y: m.t, width: band, height: H - m.t, class: "hit", tabindex: 0 }, svg);
      const open = (e) => {
        hover.classList.add("on");
        showTip(e, `${r.kvartal}${isNow ? " · nowcast" : ""}`, [
          { label: "Prognos", value: `${nf(r.prognos)} mkr · ${r.prognos_vs_konsensus}`, color: C.model },
          { label: "Konsensus", value: `${nf(r.konsensus)} mkr`, color: C.cons },
          { label: "Utfall", value: r.utfall == null ? "ej rapporterat" : `${nf(r.utfall)} mkr · ${r.utfall_vs_konsensus}`, color: C.actual },
          { label: "Blocktimmar (indata)", value: `${nf(r.blocktimmar_indata, 0)} (${pct(r.timmar_yoy)})` },
          { label: "Riktning", value: r.rätt == null ? "–" : r.rätt ? "✓ rätt" : "✗ fel", cls: r.rätt == null ? null : r.rätt ? "status-good" : "status-critical" },
        ]);
      };
      hit.addEventListener("pointermove", open);
      hit.addEventListener("focus", (e) => { const b = hit.getBoundingClientRect(); open({ clientX: b.right, clientY: b.top + 40 }); });
      const close = () => { hover.classList.remove("on"); hideTip(); };
      hit.addEventListener("pointerleave", close); hit.addEventListener("blur", close);
    });
    txt(axis, 14, m.t + (H - m.t - m.b) / 2, "mkr CAD", { transform: `rotate(-90 14 ${m.t + (H - m.t - m.b) / 2})`, "text-anchor": "middle" });

    // tabell (samma data — nås utan hover)
    const body = $("hist-body"); body.replaceChildren();
    for (const r of rows) {
      const tr = document.createElement("tr");
      if (r.timmar !== "rapporterade (MD&A)") tr.className = "nowcast";
      const cells = [
        [r.kvartal + (r.timmar !== "rapporterade (MD&A)" ? " (ADS-B)" : ""), ""],
        [r.rapportdag ?? "–", ""],
        [nf(r.blocktimmar_indata, 0), "num"], [pct(r.timmar_yoy), "num"],
        [nf(r.prognos), "num"], [nf(r.konsensus), "num"], [r.prognos_vs_konsensus, ""],
        [nf(r.utfall), "num"], [r.utfall_vs_konsensus, ""],
        [r.rätt == null ? "–" : r.rätt ? "✓ rätt" : "✗ fel", r.rätt == null ? "" : r.rätt ? "status-good" : "status-critical"],
        [r.kurs_rapportdag_pct == null ? "–" : `${r.kurs_rapportdag_pct >= 0 ? "+" : "−"}${nf(Math.abs(r.kurs_rapportdag_pct))} %`, "num"],
      ];
      for (const [v, cls] of cells) { const td = document.createElement("td"); td.textContent = v; if (cls) td.className = cls; tr.appendChild(td); }
      body.appendChild(tr);
    }
  }

  // ---------- 04 Fältrapport ----------
  function renderDaily(days) {
    const box = $("daily-chart"); box.replaceChildren();
    if (!days.length) { const p = document.createElement("p"); p.className = "dim"; p.textContent = "Inga insamlade dagar ännu — insamlaren (track) fyller på."; box.appendChild(p); return; }
    const H = 240, m = { l: 56, r: 16, t: 16, b: 40 };
    const [svg, W] = sizedSvg(box, 560, H);
    const max = Math.max(...days.map((d) => d.block_h));
    const ticks = niceTicks(0, max * 1.05, 4), yMax = ticks[ticks.length - 1];
    const ys = (v) => H - m.b - v / yMax * (H - m.t - m.b);
    const grid = el("g", { class: "grid" }, svg), axis = el("g", { class: "axis" }, svg);
    for (const t of ticks) { el("line", { x1: m.l, x2: W - m.r, y1: ys(t), y2: ys(t) }, grid); txt(axis, m.l - 8, ys(t) + 4, nf(t, 0), { "text-anchor": "end" }); }
    const band = (W - m.l - m.r) / days.length, bw = Math.max(2, Math.min(24, band - 2));
    const step = labelStep(band, 44);   // "MM-DD" i 11 px mono ≈ 38 px + luft
    days.forEach((d, i) => {
      const cx = m.l + i * band + band / 2, y = ys(d.block_h), h = H - m.b - y, r = Math.min(4, h);
      // 4px rundad dataände, rak mot baslinjen
      el("path", { d: `M${cx - bw / 2},${H - m.b} V${y + r} Q${cx - bw / 2},${y} ${cx - bw / 2 + r},${y} H${cx + bw / 2 - r} Q${cx + bw / 2},${y} ${cx + bw / 2},${y + r} V${H - m.b} Z`, fill: C.daily }, svg);
      if (i % step === 0) txt(axis, cx, H - m.b + 16, d.day.slice(5), { "text-anchor": "middle" });
      const hit = el("rect", { x: m.l + i * band, y: m.t, width: band, height: H - m.t - m.b + 20, class: "hit" }, svg);
      hit.addEventListener("pointermove", (e) => showTip(e, `${d.day} (${d.weekday})`, [
        { label: "Blocktimmar", value: nf(d.block_h), color: C.daily },
        { label: "domestic", value: nf(d.block_h_domestic) }, { label: "ACMI", value: nf(d.block_h_acmi) },
        { label: "charter", value: nf(d.block_h_charter) },
        { label: "Flygningar / plan", value: `${d.flights} / ${d.aircraft}` },
      ]));
      hit.addEventListener("pointerleave", hideTip);
    });
  }

  // ---------- Start ----------
  async function load() {
    $("app").setAttribute("aria-busy", "true");
    for (;;) {
      const res = await fetch("/api/all");
      const body = await res.json();
      if (res.status === 200) return body;
      if (body.error) throw new Error(body.error);
      await new Promise((r) => setTimeout(r, 1500));
    }
  }

  async function boot() {
    try {
      const data = await load();
      NC = data.nowcast; lastData = data;
      $("built-at").textContent = data.built_at;
      $("loading").hidden = true; $("content").hidden = false; $("app").setAttribute("aria-busy", "false");
      renderSignal(data.tests, data.nowcast);
      initSim(data.nowcast);
      renderHistory(data.tests);
      renderDaily(data.daily);
    } catch (err) {
      $("loading").querySelector(".loading-code").textContent = "ÅTKOMST NEKAD";
      $("loading").querySelector(".loading-sub").textContent = `Dossiern kunde inte byggas: ${err.message}`;
    }
  }

  // Rita om diagrammen i ny pixelbredd när fönstret ändras (debounce)
  let lastData = null, resizeTimer = null;
  addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!lastData) return;
      renderSimChart(+$("hours-slider").value / 100);
      renderHistory(lastData.tests);
      renderDaily(lastData.daily);
    }, 150);
  });

  $("refresh").addEventListener("click", async () => {
    await fetch("/api/refresh", { method: "POST" });
    $("content").style.opacity = "0.5";
    await new Promise((r) => setTimeout(r, 800));
    location.reload();
  });
  addEventListener("scroll", hideTip, { passive: true });
  boot();
})();
