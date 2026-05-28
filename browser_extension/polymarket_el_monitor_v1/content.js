(() => {
  "use strict";

  const STATE_URLS = ["http://127.0.0.1:8766/state", "http://localhost:8766/state"];
  const PANEL_ID = "pm-el-monitor-ext";
  const DOCK_CLASS = "pm-el-monitor-right-dock-active";
  const DOCK_WIDTH_PX = 300;
  const LS_KEY = "pm_el_monitor_position";
  const POLL_MS = 200;

  let lastWorkingUrl = STATE_URLS[0];

  function loadPosition() {
    try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}"); }
    catch { return {}; }
  }
  function savePosition(pos) { localStorage.setItem(LS_KEY, JSON.stringify(pos)); }
  function clearPosition() { localStorage.removeItem(LS_KEY); }

  // ---------------------------------------------------------------------------
  // Win probability — baseada em estatisticas reais dos trades (2026-05-28)
  //
  // Fontes:
  //   EE zona morta pos-FAK-removal: 20 wins / 24 trades = 83% (bid>=0.80, secs>35)
  //   PP window (bid>=0.88, secs 36-70): ~91% (profit protect historico)
  //   Near win (bid>=0.85, secs<=35): ~94% (quase todos resolvem a favor)
  //   EE entry gate vel>=0.13: WR 86% (CLAUDE.md — gate deployado 2026-05-28)
  //   EE entry gate vel<0.13:  WR 68% (abaixo do gate, nao entra)
  // ---------------------------------------------------------------------------
  function calcWinProb(side, bids, secs, ee) {
    const myBid  = side ? (side === "UP" ? (bids.up  || 0) : (bids.down || 0)) : 0;
    const oppBid = side ? (side === "UP" ? (bids.down || 0) : (bids.up  || 0)) : 0;
    if (!side) return null;

    // Reversao total: livro EL zerou + opp domina
    if (myBid <= 0 && oppBid >= 0.85) return { pct: 5,  label: "reversao total", color: "red" };
    if (myBid <= 0 && oppBid > 0)     return { pct: 15, label: "livro EL zerou", color: "red" };
    if (myBid <= 0)                   return null;

    // Near win: secs <= 35
    if (secs !== null && secs <= 35) {
      if (oppBid >= 0.85) return { pct: 8,  label: "reversao secs<=35",      color: "red" };
      if (myBid  >= 0.85) return { pct: 94, label: "near win bid>=0.85",     color: "green" };
      if (myBid  >= 0.75) return { pct: 82, label: "secs<=35 bid 0.75-0.85", color: "yellow" };
    }

    // Profit protect window (secs 36-70, bid >= 0.88)
    if (secs !== null && secs >= 36 && secs <= 70 && myBid >= 0.88)
      return { pct: 91, label: "PP window bid>=0.88", color: "green" };

    // Hedge gap
    if (myBid < 0.50 && oppBid > 0) return { pct: 22, label: "bid<0.50 hedge gap", color: "red" };

    // Zona morta (secs > 35 ou desconhecido, bid 0.50-0.84)
    // Estatistica pos-FAK-removal: 20w/4s em bid>=0.80 = 83%
    if (myBid >= 0.80) return { pct: 83, label: "zona morta bid>=0.80",      color: "yellow" };
    if (myBid >= 0.73) return { pct: 73, label: "zona morta bid 0.73-0.80",  color: "yellow" };
    if (myBid >= 0.65) return { pct: 55, label: "zona morta bid 0.65-0.73",  color: "orange" };
    if (myBid >= 0.50) return { pct: 38, label: "zona morta bid 0.50-0.65",  color: "red" };

    return null;
  }

  // Win prob para sinal de entrada (sem posicao aberta)
  function calcEntryProb(ee) {
    const vel = ee.el_vel;
    const n180 = ee.n_s180 || 0;
    if (vel == null || vel <= 0) return null;
    if (!ee.signal_ok && !ee.f3_ok) return null;
    if (vel >= 0.13 && n180 >= 3) return { pct: 86, label: "gate real: vel>=0.13 n_s180>=3", color: "green" };
    if (vel >= 0.13)               return { pct: 82, label: "vel>=0.13 (n_s180 baixo)",       color: "yellow" };
    if (vel >= 0.08)               return { pct: 68, label: "vel<0.13 (abaixo do gate)",       color: "orange" };
    return null;
  }

  // ---------------------------------------------------------------------------
  // Exit logic
  // ---------------------------------------------------------------------------
  const PP_BID     = 0.88;
  const PP_SECS_LO = 36;
  const PP_SECS_HI = 70;
  const WIN_BID    = 0.85;
  const WIN_SECS   = 35;
  const HEDGE_THR  = 0.50;

  function exitRecommendation(side, bids, secs) {
    const myBid  = side === "UP" ? (bids.up  || 0) : (bids.down || 0);
    const oppBid = side === "UP" ? (bids.down || 0) : (bids.up  || 0);

    if (myBid <= 0) {
      if (oppBid >= WIN_BID)
        return { action: "SAIDA URGENTE", color: "red",
          detail: "livro EL zerou + opp>=0.85 -- reversao total", urgent: true };
      if (oppBid > 0)
        return { action: "STOP URGENTE", color: "red",
          detail: `livro EL zerou, opp=${oppBid.toFixed(3)} -- hedge se possivel`, urgent: true };
      return { action: "SEM DADOS", color: "gray", detail: "aguardando bids" };
    }

    if (myBid < HEDGE_THR && oppBid > 0)
      return { action: "STOP URGENTE", color: "red",
        detail: `bid=${myBid.toFixed(3)} < 0.50 -- ee_hedge_gap`, urgent: true };

    if (secs !== null && secs <= WIN_SECS) {
      if (oppBid >= WIN_BID)
        return { action: "SAIDA URGENTE", color: "red",
          detail: `reversao: ${side === "UP" ? "DOWN" : "UP"}=${oppBid.toFixed(3)} >= ${WIN_BID}`, urgent: true };
      if (myBid >= WIN_BID)
        return { action: "WIN", color: "green",
          detail: `bid=${myBid.toFixed(3)} >= ${WIN_BID} -- aguardar resolucao` };
    }

    if (secs !== null && secs >= PP_SECS_LO && secs <= PP_SECS_HI && myBid >= PP_BID)
      return { action: "PROFIT PROTECT", color: "green",
        detail: `bid=${myBid.toFixed(3)} >= ${PP_BID} em ${secs}s (${PP_SECS_LO}-${PP_SECS_HI}s) -- sair GTC` };

    const inDeadZone = myBid < 0.84 && myBid >= HEDGE_THR && (secs === null || secs > 35);
    const distPP = PP_BID - myBid;
    const inPPWin = secs !== null && secs >= PP_SECS_LO && secs <= PP_SECS_HI;
    const ppNote = inPPWin
      ? `PP: falta +${distPP.toFixed(3)}`
      : (secs !== null && secs > PP_SECS_HI ? `PP em ${secs - PP_SECS_HI}s` : "PP: janela passou");

    if (inDeadZone)
      return { action: myBid < 0.70 ? "ZONA MORTA" : "SEM PROTECAO", color: myBid < 0.70 ? "yellow" : "gray",
        detail: `bid=${myBid.toFixed(3)} (0.50-0.84, secs>35) | ${ppNote}` };

    return { action: "POSICAO ABERTA", color: "gray",
      detail: `bid=${myBid.toFixed(3)} | ${ppNote} | secs=${secs ?? "?"}` };
  }

  // ---------------------------------------------------------------------------
  // Render helpers
  // ---------------------------------------------------------------------------
  function fmt(v, d = 3) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
    return Number(v).toFixed(d);
  }
  function esc(v) {
    return String(v ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  }
  function statusClass(c) {
    return `elm-status-${{ green:"green", orange:"orange", yellow:"yellow", gray:"gray", red:"red", error:"error" }[c] || "gray"}`;
  }
  function secsDisplay(secs) {
    if (secs === null || secs === undefined) return "--";
    const m = Math.floor(secs / 60), s = secs % 60;
    return m > 0 ? `${m}m${String(s).padStart(2,"0")}s` : `${s}s`;
  }
  function bidBox(label, bid, isLeader) {
    return `<div class="elm-bid-box${isLeader ? " leader" : ""}">
      <div class="elm-bid-label">${label}</div>
      <div class="elm-bid-val">${fmt(bid)}</div>
    </div>`;
  }
  function elmRow(label, val, cls = "") {
    return `<div class="elm-row">
      <span class="elm-row-label">${esc(label)}</span>
      <span class="elm-row-val${cls ? " "+cls : ""}">${esc(val)}</span>
    </div>`;
  }
  function criteriaHtml(criteria) {
    if (!Array.isArray(criteria) || !criteria.length) return "";
    return `<div class="elm-criteria">${criteria.map((c) => {
      const detail = c.detail ? `<div class="elm-criterion-detail">${esc(c.detail)}</div>` : "";
      return `<div class="elm-criterion ${c.ok ? "ok" : "block"}">
        <div class="elm-criterion-main">
          <span class="elm-dot"></span>
          <span class="elm-criterion-label">${esc(c.label)}</span>
          <span class="elm-criterion-value">${esc(c.value)}</span>
        </div>${detail}
      </div>`;
    }).join("")}</div>`;
  }

  function winProbHtml(wp) {
    if (!wp) return "";
    const bar = Math.round(wp.pct);
    const barColor = { green: "#22c55e", yellow: "#eab308", orange: "#f97316", red: "#ef4444" }[wp.color] || "#64748b";
    return `<div class="elm-win-prob">
      <div class="elm-win-prob-header">
        <span class="elm-win-prob-label">Chance de Win</span>
        <span class="elm-win-prob-pct" style="color:${barColor}">${bar}%</span>
      </div>
      <div class="elm-win-prob-bar-bg">
        <div class="elm-win-prob-bar" style="width:${bar}%;background:${barColor}"></div>
      </div>
      <div class="elm-win-prob-basis">${esc(wp.label)}</div>
    </div>`;
  }

  // ---------------------------------------------------------------------------
  // Position section HTML
  // ---------------------------------------------------------------------------
  function positionHtml(pos, state) {
    const bids = state.bids || {};
    const secs = state.secs_to_end;
    const ee   = state.ee || {};

    const btnUp = `<button class="elm-btn-entry elm-btn-entry-up${pos.side === "UP" ? " active" : ""}" data-side="UP">Entrei UP</button>`;
    const btnDn = `<button class="elm-btn-entry elm-btn-entry-dn${pos.side === "DOWN" ? " active" : ""}" data-side="DOWN">Entrei DOWN</button>`;
    const btnClear = pos.side ? `<button class="elm-btn-clear">Sair / Limpar</button>` : "";

    if (!pos.side) {
      // Sem posicao: mostrar % de win do sinal de entrada atual
      const entryProb = calcEntryProb(ee);
      return `<div class="elm-section elm-position-section">
        <div class="elm-section-title">Posicao</div>
        ${entryProb ? winProbHtml(entryProb) : ""}
        <div class="elm-pos-buttons">${btnUp}${btnDn}</div>
      </div>`;
    }

    const rec = exitRecommendation(pos.side, bids, secs);
    const myBid  = pos.side === "UP" ? (bids.up  || 0) : (bids.down || 0);
    const oppBid = pos.side === "UP" ? (bids.down || 0) : (bids.up  || 0);
    const wp = calcWinProb(pos.side, bids, secs, ee);

    return `<div class="elm-section elm-position-section elm-position-active">
      <div class="elm-section-title">Posicao -- ${esc(pos.side)}</div>
      <div class="elm-pos-exit-badge ${statusClass(rec.color)}">${esc(rec.action)}</div>
      <div class="elm-pos-detail">${esc(rec.detail)}</div>
      ${winProbHtml(wp)}
      ${elmRow("Bid " + pos.side, fmt(myBid), myBid >= 0.85 ? "ok" : myBid < 0.50 ? "bad" : "")}
      ${elmRow("Bid oposto", fmt(oppBid), oppBid >= 0.85 ? "bad" : "")}
      ${elmRow("PP (0.88, 36-70s)", myBid > 0 ? (myBid >= 0.88 ? `DISPONIVEL (${fmt(myBid)})` : `falta +${(0.88 - myBid).toFixed(3)}`) : "-", myBid >= 0.88 ? "ok" : "")}
      <div class="elm-pos-buttons elm-pos-buttons-sm">${btnClear}</div>
    </div>`;
  }

  // ---------------------------------------------------------------------------
  // Panel
  // ---------------------------------------------------------------------------
  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    document.documentElement.style.setProperty("--pm-el-monitor-dock-width", `${DOCK_WIDTH_PX}px`);
    document.documentElement.classList.add(DOCK_CLASS);
    if (panel) return panel;
    panel = document.createElement("div");
    panel.id = PANEL_ID;
    document.body.appendChild(panel);
    return panel;
  }
  function hidePanel() {
    document.getElementById(PANEL_ID)?.remove();
    document.documentElement.classList.remove(DOCK_CLASS);
  }

  // ---------------------------------------------------------------------------
  // Main render
  // ---------------------------------------------------------------------------
  function render(state) {
    const panel = ensurePanel();
    const slug   = state.slug || "";
    const secs   = state.secs_to_end;
    const action = state.action || "AGUARDAR";
    const color  = state.color || "gray";
    const detail = state.detail || "";
    const bids   = state.bids || {};
    const ee     = state.ee || {};
    const el     = state.el || {};
    const pos    = loadPosition();

    const elSide = ee.early_side || null;
    const inverted = el.inverted;

    const velVal = ee.el_vel != null
      ? `${ee.el_vel >= 0 ? "+" : ""}${fmt(ee.el_vel)} (min:0.13)` : "-";
    const velCls = ee.el_vel >= 0.13 ? "ok" : (ee.el_vel > 0 ? "warn" : "");
    const contVal = ee.f3_ok === true ? "sim" : ee.f3_ok === false ? "NAO" : "aguardando";
    const contCls = ee.f3_ok === true ? "ok" : ee.f3_ok === false ? "bad" : "";

    const invHtml = inverted ? `<div class="elm-section">
      <div class="elm-section-title">Inversao detectada</div>
      ${elmRow("Novo lider", `${el.inversion_side} bid=${fmt(el.inversion_bid)}`)}
      ${elmRow("Forca", el.inversion_strong ? "FORTE (>=0.72)" : "moderada (<0.72)", el.inversion_strong ? "ok" : "warn")}
    </div>` : "";

    panel.innerHTML = `
      <div class="elm-head">
        <div class="elm-title">EL Monitor | ${esc(slug.slice(-20) || "aguardando...")}</div>
        <div class="elm-secs">${secsDisplay(secs)}</div>
      </div>

      <div class="elm-status ${statusClass(color)}">${esc(action)}</div>
      <div class="elm-detail">${esc(detail)}</div>

      <div class="elm-bids">
        ${bidBox("UP",   bids.up,   elSide === "UP")}
        ${bidBox("DOWN", bids.down, elSide === "DOWN")}
      </div>

      ${positionHtml(pos, state)}

      <div class="elm-section">
        <div class="elm-section-title">Early Leader EE</div>
        ${elmRow("Lado",         elSide ? elSide : "nao detectado", elSide ? "ok" : "")}
        ${elmRow("bid_240",      fmt(ee.el_bid_240))}
        ${elmRow("bid_180",      fmt(ee.el_bid_180))}
        ${elmRow("Velocidade",   velVal, velCls)}
        ${elmRow("Continuidade", contVal, contCls)}
        ${elmRow("Amostras",     `${ee.n_s240 || 0}@240 / ${ee.n_s180 || 0}@180`)}
      </div>

      ${invHtml}

      ${criteriaHtml(state.criteria)}

      <div class="elm-actions">
        <button class="elm-btn-hide" id="${PANEL_ID}-hide">Fechar</button>
      </div>
    `;

    panel.querySelectorAll(".elm-btn-entry").forEach((btn) => {
      btn.addEventListener("click", () => {
        const side = btn.dataset.side;
        const cur = loadPosition();
        if (cur.side === side) clearPosition();
        else savePosition({ side, entrySlug: slug });
        render(state);
      });
    });
    panel.querySelector(".elm-btn-clear")?.addEventListener("click", () => {
      clearPosition(); render(state);
    });
    panel.querySelector(`#${PANEL_ID}-hide`)?.addEventListener("click", hidePanel);
  }

  function renderError(msg) {
    render({
      slug: null, secs_to_end: null, action: "SEM CONEXAO", color: "error",
      detail: msg, bids: {}, ee: {}, el: {},
      criteria: [{ label: "Servidor local", value: "falhou", ok: false,
        detail: "rode: python run_el_monitor_server.py" }],
    });
  }

  // ---------------------------------------------------------------------------
  // Fetch — direto primeiro (sem overhead de service worker), fallback via background
  // ---------------------------------------------------------------------------
  async function fetchDirect() {
    const urls = [lastWorkingUrl, ...STATE_URLS.filter((u) => u !== lastWorkingUrl)];
    for (const url of urls) {
      try {
        const res = await fetch(url, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        lastWorkingUrl = url;
        return await res.json();
      } catch (_) {}
    }
    return null;
  }

  async function fetchState() {
    // Tenta fetch direto primeiro (mais rapido — sem round-trip via service worker)
    const direct = await fetchDirect();
    if (direct) return direct;

    // Fallback: via background service worker
    if (typeof chrome !== "undefined" && chrome.runtime?.sendMessage) {
      try {
        const resp = await chrome.runtime.sendMessage({ type: "pm_el_monitor_state" });
        if (resp?.ok && resp.state) return resp.state;
      } catch (_) {}
    }
    throw new Error("servidor indisponivel -- porta 8766");
  }

  async function tick() {
    try {
      const state = await fetchState();
      if (state.status === "error") renderError(state.error || "erro no servidor");
      else render(state);
    } catch (err) {
      renderError(String(err?.message || err || "servidor indisponivel -- porta 8766"));
    }
  }

  setInterval(tick, POLL_MS);
  tick();
})();
