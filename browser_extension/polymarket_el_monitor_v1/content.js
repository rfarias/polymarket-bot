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
  // Win probability — baseada em estatisticas reais (2026-05-28)
  //
  // EE zona morta pos-FAK-removal: 20w/4s em bid>=0.80 = 83%
  // PP window (bid>=0.88, secs 36-70): ~91%
  // Near win (bid>=0.85, secs<=35): ~94%
  // EE entry gate vel>=0.13: WR 86% (gate deployado 2026-05-28)
  // AR: baseado em bid atual (quanto mais perto de 1.0, maior chance)
  // ---------------------------------------------------------------------------
  function calcWinProbEE(side, bids, secs) {
    const myBid  = side ? (side === "UP" ? (bids.up  || 0) : (bids.down || 0)) : 0;
    const oppBid = side ? (side === "UP" ? (bids.down || 0) : (bids.up  || 0)) : 0;
    if (!side) return null;

    if (myBid <= 0 && oppBid >= 0.85) return { pct: 5,  label: "reversao total",      color: "red" };
    if (myBid <= 0 && oppBid > 0)     return { pct: 15, label: "livro EL zerou",       color: "red" };
    if (myBid <= 0)                   return null;

    if (secs !== null && secs <= 35) {
      if (oppBid >= 0.85) return { pct: 8,  label: "reversao secs<=35",      color: "red" };
      if (myBid  >= 0.85) return { pct: 94, label: "near win bid>=0.85",     color: "green" };
      if (myBid  >= 0.75) return { pct: 82, label: "secs<=35 bid 0.75-0.85", color: "yellow" };
    }

    if (secs !== null && secs >= 36 && secs <= 70 && myBid >= 0.88)
      return { pct: 91, label: "PP window bid>=0.88", color: "green" };

    if (myBid < 0.50 && oppBid > 0) return { pct: 22, label: "bid<0.50 hedge gap", color: "red" };

    // Zona morta: pos-FAK-removal: 20/24 = 83% bid>=0.80
    if (myBid >= 0.80) return { pct: 83, label: "zona morta bid>=0.80",     color: "yellow" };
    if (myBid >= 0.73) return { pct: 73, label: "zona morta bid 0.73-0.80", color: "yellow" };
    if (myBid >= 0.65) return { pct: 55, label: "zona morta bid 0.65-0.73", color: "orange" };
    if (myBid >= 0.50) return { pct: 38, label: "zona morta bid 0.50-0.65", color: "red" };
    return null;
  }

  function calcWinProbAR(ep, currentBid) {
    // AR: chance de win baseada no bid atual (distancia de 1.0)
    // Dados: ep 0.95-0.96 avg=-0.014 (break even), ep 0.97-0.98 avg=+0.06
    // Gate bloqueia ep>=0.97 (standard), variante gate>=0.985 — so chegamos aqui com ep 0.95-0.96
    const bid = currentBid || ep;
    if (bid >= 0.995) return { pct: 98, label: "AR bid>=0.995 resolucao",   color: "green" };
    if (bid >= 0.98)  return { pct: 93, label: "AR bid>=0.98",              color: "green" };
    if (bid >= 0.96)  return { pct: 86, label: "AR bid>=0.96",              color: "green" };
    if (bid >= 0.94)  return { pct: 76, label: "AR bid>=0.94",              color: "yellow" };
    if (bid >= 0.90)  return { pct: 63, label: "AR bid>=0.90",              color: "yellow" };
    if (bid >= ep)    return { pct: 50, label: "AR bid>=ep neutro",          color: "orange" };
    return             { pct: 28, label: "AR bid<ep contra posicao",        color: "red" };
  }

  function calcWinProb(side, bids, secs, runner) {
    if (!side) return null;
    if (runner && runner.mode === "open_position") {
      const variant = runner.setup_variant || "";
      if (variant === "early_entry") {
        // EE: usa bid atual da extensao (mais fresco que o runner state file)
        return calcWinProbEE(side, bids, secs);
      }
      // AR: usa current_bid do runner (bid do mercado da posicao aberta)
      return calcWinProbAR(runner.ep || 0, runner.current_bid || 0);
    }
    // Posicao manual sem runner: EE logic
    return calcWinProbEE(side, bids, secs);
  }

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
  // Exit logic (espelha runner v2 2026-05-28)
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
  function fmtPnl(v) {
    if (v === null || v === undefined) return "-";
    const n = Number(v);
    return (n >= 0 ? "+" : "") + n.toFixed(2);
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
  // Runner section — estado do runner real
  // ---------------------------------------------------------------------------
  const RUNNER_MODE_LABELS = {
    idle:                 ["IDLE",           "gray"],
    pending_entry:        ["ENTRANDO",        "yellow"],
    open_position:        ["POSICAO ABERTA", "green"],
    pending_exit:         ["SAINDO",          "yellow"],
    exit_pending_confirm: ["AGUARD. CONFIRM", "yellow"],
    awaiting_redeem:      ["AGUARD. REDEEM",  "orange"],
  };

  function runnerSectionHtml(runner, bids, secs) {
    if (!runner) {
      return `<div class="elm-section elm-runner-section">
        <div class="elm-section-title">Runner</div>
        <div class="elm-runner-offline">sem dados (runner parado ou outro PC)</div>
      </div>`;
    }

    const mode = runner.mode || "idle";
    const [modeLabel, modeColor] = RUNNER_MODE_LABELS[mode] || [mode.toUpperCase(), "gray"];
    const variant = runner.setup_variant || "";
    const isEE = variant === "early_entry";
    const isAR = !isEE && mode !== "idle";
    const side = runner.side || "";
    const ep = runner.ep || 0;
    const qty = runner.qty || 0;
    const remQty = runner.remaining_qty || qty;
    const rBid = runner.current_bid || 0;
    const unreal = runner.unrealized_pnl;
    const rsecs = runner.current_secs;

    // Win probability: para posicao aberta
    let wp = null;
    if (mode === "open_position" && side) {
      if (isEE) {
        wp = calcWinProbEE(side, bids, rsecs != null ? rsecs : secs);
      } else {
        wp = calcWinProbAR(ep, rBid);
      }
    }

    // AR signal
    const arSig = runner.ar_signal || {};
    const arAllow = arSig.allow;
    const arReason = arSig.reason || "";
    const arVariant = arSig.variant || "";
    const arEp = arSig.ep;

    // EE gates
    const ee = runner.ee || {};
    const vel = ee.el_vel;
    const n180 = ee.n_s180 || 0;
    const velOk = vel != null && vel >= 0.13;
    const n180Ok = n180 >= 3;
    const eeSignal = ee.signal_ok;

    const modeColorCss = { green:"green", yellow:"yellow", orange:"orange", gray:"gray", red:"red" }[modeColor] || "gray";

    let posHtml = "";
    if (mode === "open_position" || mode === "pending_exit" || mode === "exit_pending_confirm") {
      const pnlCls = unreal === null ? "" : (unreal >= 0 ? "ok" : "bad");
      const variantShort = isEE ? "EE" : (variant || "AR").toUpperCase().slice(0,8);
      posHtml = `
        ${winProbHtml(wp)}
        ${elmRow("Lado / Variant", `${side} [${variantShort}]`, side ? "ok" : "")}
        ${elmRow("Entry price",    fmt(ep), "")}
        ${elmRow("Qty restante",   `${remQty} / ${qty}`, "")}
        ${elmRow("Bid atual",      rBid > 0 ? fmt(rBid) : "-", rBid >= ep ? "ok" : rBid > 0 ? "warn" : "")}
        ${elmRow("PnL nao real.",  fmtPnl(unreal), pnlCls)}
        ${rsecs != null ? elmRow("Secs (runner)", `${secsDisplay(rsecs)}`, "") : ""}
      `;
    } else if (mode === "awaiting_redeem") {
      posHtml = `
        ${elmRow("Lado", side || "-", "")}
        ${elmRow("Entry price", fmt(ep), "")}
        ${elmRow("Motivo", runner.last_reason || "-", "")}
      `;
    }

    // AR signal row
    let arHtml = "";
    if (mode === "idle") {
      const arCls = arAllow ? "ok" : "warn";
      const arText = arAllow
        ? `SINAL: ${arSig.side || "?"} ep=${fmt(arEp, 3)} [${arVariant}]`
        : `bloqueado: ${arReason.slice(0, 35)}`;
      arHtml = elmRow("AR sinal", arText, arCls);
    }

    // EE gates row
    const velTxt = vel != null ? `${vel >= 0 ? "+" : ""}${vel.toFixed(3)} ${velOk ? "ok" : "baixo"}` : "-";
    const eeSigTxt = eeSignal ? "SINAL EE ativo" : (vel != null && vel >= 0.13 ? "fora da janela" : "aguardando");
    const eeSigCls = eeSignal ? "ok" : "";

    return `<div class="elm-section elm-runner-section">
      <div class="elm-section-title">Runner</div>
      <div class="elm-runner-badge elm-runner-${modeColorCss}">${modeLabel}</div>
      ${posHtml}
      ${arHtml}
      <div class="elm-section-title" style="margin-top:5px">EE Gates</div>
      ${elmRow("EL vel", velTxt, velOk ? "ok" : vel != null ? "warn" : "")}
      ${elmRow("n_s180", `${n180} ${n180Ok ? "ok" : n180 > 0 ? "(baixo)" : "-"}`, n180Ok ? "ok" : n180 > 0 ? "warn" : "")}
      ${elmRow("EE sinal", eeSigTxt, eeSigCls)}
    </div>`;
  }

  // ---------------------------------------------------------------------------
  // Manual position section
  // ---------------------------------------------------------------------------
  function positionHtml(pos, state, runner) {
    const bids = state.bids || {};
    const secs = state.secs_to_end;
    const ee   = state.ee || {};

    const btnUp = `<button class="elm-btn-entry elm-btn-entry-up${pos.side === "UP" ? " active" : ""}" data-side="UP">Entrei UP</button>`;
    const btnDn = `<button class="elm-btn-entry elm-btn-entry-dn${pos.side === "DOWN" ? " active" : ""}" data-side="DOWN">Entrei DOWN</button>`;
    const btnClear = pos.side ? `<button class="elm-btn-clear">Sair / Limpar</button>` : "";

    if (!pos.side) {
      const entryProb = calcEntryProb(ee);
      return `<div class="elm-section elm-position-section">
        <div class="elm-section-title">Posicao manual</div>
        ${entryProb ? winProbHtml(entryProb) : ""}
        <div class="elm-pos-buttons">${btnUp}${btnDn}</div>
      </div>`;
    }

    const rec = exitRecommendation(pos.side, bids, secs);
    const myBid  = pos.side === "UP" ? (bids.up  || 0) : (bids.down || 0);
    const wp = calcWinProb(pos.side, bids, secs, runner);

    return `<div class="elm-section elm-position-section elm-position-active">
      <div class="elm-section-title">Posicao manual -- ${esc(pos.side)}</div>
      <div class="elm-pos-exit-badge ${statusClass(rec.color)}">${esc(rec.action)}</div>
      <div class="elm-pos-detail">${esc(rec.detail)}</div>
      ${winProbHtml(wp)}
      ${elmRow("Bid " + pos.side, fmt(myBid), myBid >= 0.85 ? "ok" : myBid < 0.50 ? "bad" : "")}
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
    const runner = state.runner || null;
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

      ${runnerSectionHtml(runner, bids, secs)}

      ${positionHtml(pos, state, runner)}

      <div class="elm-section">
        <div class="elm-section-title">Early Leader EE (servidor)</div>
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
      detail: msg, bids: {}, ee: {}, el: {}, runner: null,
      criteria: [{ label: "Servidor local", value: "falhou", ok: false,
        detail: "rode: python run_el_monitor_server.py" }],
    });
  }

  // ---------------------------------------------------------------------------
  // Fetch — direto primeiro (sem overhead de service worker)
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
    const direct = await fetchDirect();
    if (direct) return direct;
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
