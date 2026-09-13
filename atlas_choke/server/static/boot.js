/* ============================================================================
   ATLAS CARGOJET TRACKER — uppstartsskärm. Nedärvd från atlas-earth/chokepoint:
   mäter FAKTISKA API-anrop (hakar i window.fetch), fyller siffror och kedjor
   med riktig data ur svaren och släpper igenom när trackerns kritiska
   resurser är klara. Laddas FÖRE övriga skript.
   ========================================================================== */

(() => {
  "use strict";

  const CRITICAL = ["/api/trackers", "/history", "/current"];
  // Signal till tracker.js: simuleringen autostartar när skärmen släpper.
  window.atlasBoot = true;
  const HARD_TIMEOUT_MS = 22000;
  const MIN_SHOW_MS = 4200;
  const CHAIN_MS = 1150;

  const state = {
    started: 0, done: 0, failed: 0,
    critical: new Set(), t0: performance.now(),
    dismissed: false, cesium: false,
  };

  const el = document.createElement("div");
  el.id = "atlas-boot";
  el.innerHTML = `
    <div class="boot-scan"></div>
    <div class="boot-vignette"></div>
    <div class="boot-inner">
      <div class="boot-head">
        <div class="boot-classified">◤ CARGOJET TRACKER ◢ &nbsp;ADS-B → BLOCKTIMMAR → KONSENSUS</div>
        <h1 class="boot-title" data-txt="ATLAS">ATLAS</h1>
        <div class="boot-sub">TSX: CJT &nbsp;·&nbsp; FLYGFRAKT → MARKNADEN &nbsp;·&nbsp; TERMINAL 01</div>
      </div>

      <div class="boot-stats" id="boot-stats">
        <div class="stat"><b data-key="fleet">0</b><span>PLAN I FLOTTAN</span></div>
        <div class="stat"><b data-key="quarters">0</b><span>KVARTAL I BACKTEST</span></div>
        <div class="stat"><b data-key="hits">0</b><span>RÄTT RIKTNING</span></div>
        <div class="stat"><b data-key="hours">0</b><span id="boot-hours-label">FLYGTIMMAR HITTILLS</span></div>
        <div class="stat"><b data-key="days">0</b><span>DYGN MED ADS-B</span></div>
      </div>

      <div class="boot-mid">
        <div class="boot-radar">
          <div class="radar-grid"></div>
          <div class="radar-ring r1"></div>
          <div class="radar-ring r2"></div>
          <div class="radar-ring r3"></div>
          <div class="radar-sweep"></div>
          <div class="radar-blip b1"></div>
          <div class="radar-blip b2"></div>
          <div class="radar-blip b3"></div>
        </div>
        <div class="boot-chains">
          <div class="chains-head">TRANSMISSIONSKEDJOR<span id="boot-chain-tag">SIGNALFLÖDE</span></div>
          <div class="chain-stack" id="boot-chains"></div>
        </div>
      </div>

      <div class="boot-foot">
        <div class="boot-log" id="boot-log"></div>
        <div class="boot-bar"><div class="boot-fill" id="boot-fill"></div>
          <div class="boot-blocks" id="boot-blocks"></div></div>
        <div class="boot-meta">
          <span id="boot-phase">INITIERAR KÄRNSYSTEM</span>
          <span id="boot-pct">00%</span>
        </div>
        <div class="boot-coords" id="boot-coords"></div>
        <div class="boot-skip">▸ KLICKA FÖR ATT GÅ IN</div>
      </div>
    </div>`;

  const style = document.createElement("style");
  style.textContent = `
  #atlas-boot { position:fixed; inset:0; z-index:9999; background:#02050a;
    color:#00ffc8; font-family:"Share Tech Mono",Consolas,monospace;
    display:flex; align-items:center; justify-content:center; overflow:hidden;
    transition:opacity .55s ease, filter .55s ease; }
  #atlas-boot.done { opacity:0; filter:brightness(2.4) blur(7px); pointer-events:none; }
  #atlas-boot::before { content:""; position:absolute; inset:0;
    background:radial-gradient(ellipse at 50% 45%, rgba(0,255,200,.09), transparent 62%); }
  .boot-scan { position:absolute; inset:0; pointer-events:none; opacity:.5;
    background:repeating-linear-gradient(0deg, rgba(0,255,200,.05) 0 1px, transparent 1px 3px);
    animation:scanmove 7s linear infinite; }
  @keyframes scanmove { to { transform:translateY(3px); } }
  .boot-vignette { position:absolute; inset:0; pointer-events:none;
    box-shadow:inset 0 0 220px 60px rgba(0,0,0,.95); }
  .boot-inner { position:relative; width:min(880px,92vw); }
  .boot-head { text-align:center; margin-bottom:26px; }
  .boot-classified { font-size:10.5px; letter-spacing:5px; color:#ff8c42;
    opacity:.85; animation:flick 3.5s infinite; }
  @keyframes flick { 0%,97%,100%{opacity:.85} 98%{opacity:.25} }
  .boot-title { position:relative; margin:8px 0 4px; font-size:clamp(44px,9vw,86px);
    letter-spacing:18px; font-weight:400; text-shadow:0 0 26px rgba(0,255,200,.55);
    animation:bootpulse 3.4s ease-in-out infinite; }
  @keyframes bootpulse { 50% { text-shadow:0 0 44px rgba(0,255,200,.85); } }
  .boot-title::before, .boot-title::after { content:attr(data-txt); position:absolute; inset:0; }
  .boot-title::before { color:#ff2e63; animation:gl1 3.1s infinite steps(1); }
  .boot-title::after  { color:#4dc3ff; animation:gl2 2.7s infinite steps(1); }
  @keyframes gl1 { 0%,94%,100%{clip-path:inset(100% 0 0 0);transform:none}
    95%{clip-path:inset(12% 0 62% 0);transform:translateX(-4px)}
    97%{clip-path:inset(58% 0 22% 0);transform:translateX(3px)} }
  @keyframes gl2 { 0%,92%,100%{clip-path:inset(100% 0 0 0);transform:none}
    93%{clip-path:inset(38% 0 42% 0);transform:translateX(4px)}
    96%{clip-path:inset(72% 0 8% 0);transform:translateX(-3px)} }
  .boot-sub { font-size:11px; letter-spacing:6px; color:#5f7f8c; }
  .boot-mid { display:grid; grid-template-columns:180px 1fr; gap:26px;
    align-items:center; margin-bottom:24px; }
  @media (max-width:620px){ .boot-mid { grid-template-columns:1fr; }
    .boot-radar { margin:0 auto; width:132px; height:132px; }
    .boot-chains { border-left:none; padding-left:0; border-top:1px solid rgba(0,255,200,.2);
      padding-top:12px; }
    .chain-stack { height:132px; gap:8px; }
    .chain { font-size:9.5px; gap:4px; }
    .chain .nd { padding:2px 6px; }
    .boot-log { height:34px; font-size:9.5px; } }
  .boot-radar { position:relative; width:180px; height:180px; }
  .radar-grid { position:absolute; inset:0; border-radius:50%;
    background:
      linear-gradient(90deg, transparent 49.6%, rgba(0,255,200,.22) 50%, transparent 50.4%),
      linear-gradient(0deg,  transparent 49.6%, rgba(0,255,200,.22) 50%, transparent 50.4%);
    border:1px solid rgba(0,255,200,.28); }
  .radar-ring { position:absolute; border:1px solid rgba(0,255,200,.16);
    border-radius:50%; top:50%; left:50%; transform:translate(-50%,-50%); }
  .r1 { width:33%; height:33%; } .r2 { width:66%; height:66%; } .r3 { width:99%; height:99%; }
  .radar-sweep { position:absolute; inset:0; border-radius:50%;
    background:conic-gradient(from 0deg, rgba(0,255,200,.42), rgba(0,255,200,.05) 42deg,
      transparent 96deg); animation:sweep 2.6s linear infinite; mix-blend-mode:screen; }
  @keyframes sweep { to { transform:rotate(360deg); } }
  .radar-blip { position:absolute; width:5px; height:5px; border-radius:50%;
    background:#2aff9e; box-shadow:0 0 9px #2aff9e; opacity:0;
    animation:blip 2.6s linear infinite; }
  .b1 { top:29%; left:64%; animation-delay:.35s; }
  .b2 { top:66%; left:38%; animation-delay:1.25s; }
  .b3 { top:47%; left:76%; animation-delay:2.0s; }
  @keyframes blip { 0%{opacity:1;transform:scale(1.5)} 45%{opacity:.55} 100%{opacity:0} }
  .boot-chains { flex:1; min-width:0; display:flex; flex-direction:column;
    border-left:1px solid rgba(0,255,200,.2); padding-left:16px; }
  .chains-head { font-size:9.5px; letter-spacing:3px; color:#38505c; margin-bottom:11px;
    display:flex; align-items:center; gap:9px; white-space:nowrap; }
  .chains-head span { font-size:8px; letter-spacing:1.5px; color:#2a4652;
    border:1px solid rgba(0,255,200,.16); padding:1px 5px; border-radius:2px; }
  .chain-stack { height:172px; overflow:hidden; display:flex; flex-direction:column;
    justify-content:flex-end; gap:10px; }
  .chain { display:flex; align-items:center; flex-wrap:wrap; gap:5px; font-size:10.5px;
    opacity:0; animation:chainIn .45s ease forwards; }
  .chain.old { opacity:.25; filter:saturate(.35); transition:opacity .7s, filter .7s; }
  @keyframes chainIn { from{opacity:0;transform:translateY(12px);} to{opacity:1;transform:none;} }
  .chain .dom { font-size:8px; letter-spacing:1.8px; color:#02050a; background:#00ffc8;
    padding:2px 6px; border-radius:2px; font-weight:700; box-shadow:0 0 10px rgba(0,255,200,.5); }
  .chain .nd { border:1px solid rgba(0,255,200,.28); background:rgba(0,255,200,.05);
    padding:2px 8px; border-radius:2px; color:#8fd8c6; white-space:nowrap;
    opacity:0; animation:ndIn .3s ease forwards; }
  .chain .ar { color:#00ffc8; opacity:0; animation:ndIn .3s ease forwards;
    text-shadow:0 0 9px rgba(0,255,200,.85); font-weight:700; }
  @keyframes ndIn { from{opacity:0;transform:translateX(-7px) scale(.92);} to{opacity:1;transform:none;} }
  .chain .up { color:#2aff9e; border-color:rgba(42,255,158,.45); background:rgba(42,255,158,.07); }
  .chain .dn { color:#ff6b6b; border-color:rgba(255,77,77,.4); background:rgba(255,77,77,.06); }
  .boot-log { height:52px; overflow:hidden; font-size:10.5px; line-height:1.62;
    margin-bottom:12px; color:#4a6470; }
  .boot-log div { white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    animation:linein .28s ease; }
  @keyframes linein { from { opacity:0; transform:translateX(-9px); } }
  .boot-log .ok  { color:#2aff9e; } .boot-log .warn { color:#ffb454; }
  .boot-log .err { color:#ff4d4d; } .boot-log .dim { color:#4a6470; }
  .boot-bar { position:relative; height:16px; border:1px solid rgba(0,255,200,.35);
    background:rgba(0,255,200,.04); overflow:hidden; }
  .boot-fill { height:100%; width:0%; background:linear-gradient(90deg,#008f72,#00ffc8);
    box-shadow:0 0 20px rgba(0,255,200,.65); transition:width .35s cubic-bezier(.4,0,.2,1); }
  .boot-blocks { position:absolute; inset:0; pointer-events:none;
    background:repeating-linear-gradient(90deg, transparent 0 9px, #02050a 9px 11px); }
  .boot-meta { display:flex; justify-content:space-between; margin-top:9px;
    font-size:11.5px; letter-spacing:2.5px; }
  #boot-pct { color:#fff; text-shadow:0 0 12px rgba(0,255,200,.9); }
  .boot-coords { margin-top:7px; font-size:10px; letter-spacing:1.6px; color:#38505c; }
  .boot-grant { color:#2aff9e !important; letter-spacing:5px;
    text-shadow:0 0 18px rgba(42,255,158,.9); }
  .boot-skip { margin-top:12px; text-align:center; font-size:9.5px; letter-spacing:3px;
    color:#38505c; animation:skipPulse 2.4s ease-in-out infinite; }
  @keyframes skipPulse { 0%,100%{opacity:.35;} 50%{opacity:.9;} }
  .boot-stats { display:flex; flex-wrap:wrap; gap:10px 26px; margin:16px 0 4px;
    padding-bottom:14px; border-bottom:1px solid rgba(0,255,200,.12); }
  .boot-stats .stat { display:flex; flex-direction:column; line-height:1.1; }
  .boot-stats .stat b { font-family:"Share Tech Mono",monospace; font-size:22px;
    color:#00ffc8; text-shadow:0 0 12px rgba(0,255,200,.5); font-weight:400; }
  .boot-stats .stat span { font-size:8.5px; letter-spacing:2px; color:#4a6470; margin-top:3px; }
  @media (max-width:620px){ .boot-stats { gap:8px 18px; }
    .boot-stats .stat b { font-size:17px; } }
  .radar-blip.dyn { animation:blipDyn 2s ease-out forwards; }
  @keyframes blipDyn { 0%{opacity:1;transform:scale(2.2);} 40%{opacity:.7;}
    100%{opacity:0;transform:scale(.7);} }
  `;

  document.head.appendChild(style);
  const pendingStats = {};
  const mount = () => {
    document.body.insertBefore(el, document.body.firstChild);
    Object.entries(pendingStats).forEach(([k, v]) => setStat(k, v));
    pushChain();
    setTimeout(pushChain, 480);
  };
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);

  el.style.cursor = "pointer";
  el.addEventListener("click", () => dismiss());

  // ---------- Siffror ur riktiga API-svar ----------
  function setStat(key, value) {
    if (!Number.isFinite(value)) return;
    const b = el.querySelector(`#boot-stats b[data-key="${key}"]`);
    if (!b || !b.isConnected) { pendingStats[key] = value; return; }
    const from = parseInt(String(b.textContent).replace(/\D/g, ""), 10) || 0;
    const dur = 1100 + Math.random() * 700;
    const t0 = performance.now();
    const step = (now) => {
      const p = Math.min(1, (now - t0) / dur);
      const v = Math.round(from + (value - from) * (1 - Math.pow(1 - p, 3)));
      b.textContent = v.toLocaleString("sv-SE");
      if (p < 1 && !state.dismissed) requestAnimationFrame(step);
      else b.textContent = Math.round(value).toLocaleString("sv-SE");
    };
    requestAnimationFrame(step);
  }

  // Generiska kedjor tills backtesten har laddats — ersätts då av riktiga kvartal.
  let CHAINS = [
    ["ADS-B", "CARGOJET-FLOTTAN", "FLYGTIMMAR", "BLOCKTIMMAR", "INTÄKT"],
    ["MODELL", "TIMMAR × RATER", "PROGNOS", "VS KONSENSUS", "SIGNAL"],
    ["LIVE", "ADSB.LOL + ADSB.FI", "SPÅR PER PLAN", "KVARTAL HITTILLS", "HIGH / LOW"],
  ];

  function absorb(url, d) {
    if (!d || typeof d !== "object") return;
    if (url.includes("/api/cargojet/live")) {
      setStat("fleet", d.fleet_size);
    } else if (url.includes("/api/tracker/") && url.includes("/history")) {
      setStat("quarters", d.total);
      setStat("hits", d.hits);
      if (Array.isArray(d.items) && d.items.length) {
        CHAINS = d.items.map((it) => [
          `${it.quarter} ${it.year}`,
          `KONS ${Number(it.consensus_mcad).toFixed(1)}`,
          `TRACKER ${it.signal} ${it.signal === "HIGHER" ? "▲" : "▼"}`,
          `UTFALL ${Number(it.result_mcad).toFixed(1)} ${it.result_vs_consensus === "HIGHER" ? "▲" : "▼"}`,
          it.correct ? "RÄTT ✓" : "FEL ✗",
        ]);
        const tag = document.getElementById("boot-chain-tag");
        if (tag) tag.textContent = `BACKTEST ${d.hits}/${d.total}`;
      }
    } else if (url.includes("/api/tracker/") && url.includes("/current")) {
      setStat("hours", d.hours);
      setStat("days", d.covered_days);
      const label = document.getElementById("boot-hours-label");
      if (label && d.quarter) label.textContent = `FLYGTIMMAR ${d.quarter} ${d.year}`;
    }
  }

  function fireBlip() {
    const radar = document.querySelector(".boot-radar");
    if (!radar) return;
    const b = document.createElement("div");
    b.className = "radar-blip dyn";
    b.style.top = (14 + Math.random() * 72) + "%";
    b.style.left = (14 + Math.random() * 72) + "%";
    radar.appendChild(b);
    setTimeout(() => b.remove(), 2000);
  }

  const logEl = () => document.getElementById("boot-log");
  function log(text, cls = "") {
    const box = logEl();
    if (!box) return;
    const d = document.createElement("div");
    d.className = cls;
    d.textContent = text;
    box.appendChild(d);
    while (box.childElementCount > 3) box.removeChild(box.firstChild);
  }

  const short = (u) => String(u).replace(/^.*\/api\//, "").split("?")[0].slice(0, 34);

  let chainIdx = 0;
  function pushChain() {
    const box = document.getElementById("boot-chains");
    if (!box || state.dismissed) return;
    const c = CHAINS[chainIdx++ % CHAINS.length];
    [...box.children].forEach((n) => n.classList.add("old"));
    const row = document.createElement("div");
    row.className = "chain";
    const dom = document.createElement("span");
    dom.className = "dom";
    dom.textContent = c[0];
    row.appendChild(dom);
    c.slice(1).forEach((txt, i) => {
      if (i > 0) {
        const a = document.createElement("span");
        a.className = "ar";
        a.textContent = "→";
        a.style.animationDelay = (0.14 + i * 0.2) + "s";
        row.appendChild(a);
      }
      const up = txt.includes("▲") || txt.includes("✓");
      const dn = txt.includes("▼") || txt.includes("✗");
      const n = document.createElement("span");
      n.className = "nd" + (up ? " up" : dn ? " dn" : "");
      n.textContent = txt;
      n.style.animationDelay = (0.22 + i * 0.2) + "s";
      row.appendChild(n);
    });
    box.appendChild(row);
    while (box.childElementCount > 3) box.removeChild(box.firstChild);
    fireBlip();
  }

  const chainTimer = setInterval(pushChain, CHAIN_MS);

  function setPct(p, phase) {
    const f = document.getElementById("boot-fill");
    const t = document.getElementById("boot-pct");
    const ph = document.getElementById("boot-phase");
    if (f) f.style.width = p.toFixed(1) + "%";
    if (t) t.textContent = String(Math.floor(p)).padStart(2, "0") + "%";
    if (ph && phase) ph.textContent = phase;
  }

  function phaseFor(p) {
    if (p < 18) return "UPPRÄTTAR FÖRBINDELSE";
    if (p < 38) return "SYNKRONISERAR CARGOJET-FLOTTAN";
    if (p < 58) return "LADDAR BACKTEST";
    if (p < 78) return "RÄKNAR FLYGTIMMAR HITTILLS";
    if (p < 96) return "KALIBRERAR GLOBEN";
    return "SYSTEM KLART";
  }

  function progress() {
    const crit = CRITICAL.filter((c) => state.critical.has(c)).length;
    const critPart = (crit / CRITICAL.length) * 46;
    const restPart = state.started
      ? Math.min(1, state.done / Math.max(state.started, 4)) * 42 : 0;
    const cesiumPart = state.cesium ? 12 : 0;
    return Math.min(99.4, critPart + restPart + cesiumPart);
  }

  function tick() {
    if (state.dismissed) return;
    const p = progress();
    setPct(p, phaseFor(p));
    const c = document.getElementById("boot-coords");
    if (c) {
      const lat = (43.1736 + Math.sin(performance.now() / 2400) * 12).toFixed(4);
      const lon = (-79.935 + Math.cos(performance.now() / 3100) * 40).toFixed(4);
      c.textContent = `LAT ${lat}  LON ${lon}  ·  ${state.done}/${state.started} KANALER  ·  `
        + `SESSION ${(state.t0 | 0).toString(16).toUpperCase().slice(-6)}`;
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  const origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const isApi = url.includes("/api/");
    if (isApi && !state.dismissed) {
      state.started++;
      log("▸ " + short(url), "dim");
    }
    const t = performance.now();
    return origFetch(input, init).then((r) => {
      if (isApi && !state.dismissed) {
        state.done++;
        const ms = Math.round(performance.now() - t);
        log(`  ${r.ok ? "OK " : "!! "}${short(url)}  ${ms} ms`, r.ok ? "ok" : "warn");
        CRITICAL.forEach((c) => { if (url.includes(c)) state.critical.add(c); });
        if (r.ok) r.clone().json().then((d) => absorb(url, d)).catch(() => {});
        maybeDismiss();
      }
      return r;
    }).catch((e) => {
      if (isApi && !state.dismissed) {
        state.done++; state.failed++;
        log("  XX " + short(url) + "  ingen kontakt", "err");
        CRITICAL.forEach((c) => { if (url.includes(c)) state.critical.add(c); });
        maybeDismiss();
      }
      throw e;
    });
  };

  function maybeDismiss() {
    if (state.dismissed) return;
    const critDone = CRITICAL.every((c) => state.critical.has(c));
    if (critDone && state.cesium && performance.now() - state.t0 > MIN_SHOW_MS) dismiss();
  }

  function dismiss() {
    if (state.dismissed) return;
    state.dismissed = true;
    clearInterval(chainTimer);
    setPct(100, "SYSTEM KLART");
    log("", "");
    log("◤ ÅTKOMST BEVILJAD ◢", "grant boot-grant");
    setTimeout(() => {
      el.classList.add("done");
      window.atlasBootDone = true;
      window.dispatchEvent(new Event("atlas:boot-done"));
      setTimeout(() => el.remove(), 650);
    }, 620);
  }

  const dismissWatch = setInterval(() => {
    maybeDismiss();
    if (state.dismissed) clearInterval(dismissWatch);
  }, 250);

  const cesiumWatch = setInterval(() => {
    if (window.Cesium) {
      state.cesium = true;
      log("  OK cesium-motor initierad", "ok");
      clearInterval(cesiumWatch);
      maybeDismiss();
    }
  }, 120);

  setTimeout(() => {
    if (!state.dismissed) {
      log("  !! långsam kanal — fortsätter i bakgrunden", "warn");
      dismiss();
    }
  }, HARD_TIMEOUT_MS);

  log("ATLAS CARGOJET TRACKER — uppstart", "");
  log("▸ autentiserar mot lokal kärna …", "dim");
})();
