(() => {
  "use strict";

  const STATE_URLS = ["http://127.0.0.1:8766/state", "http://localhost:8766/state"];
  const PANEL_ID = "pm-el-monitor-ext";
  const DOCK_CLASS = "pm-el-monitor-right-dock-active";
  const DOCK_WIDTH_PX = 300;
  const POLL_MS = 200;

  let lastWorkingUrl = STATE_URLS[0];

  // ---------------------------------------------------------------------------
  // Win probability — baseada em estatisticas reais
  // 1368 slugs reais observados (2026-05-28) — sec 24 TESTES_ANALISE_EL.md
  // ---------------------------------------------------------------------------
  function calcWinProb(side, bids, secs) {
    const myBid  = side === "UP" ? (bids.up  || 0) : (bids.down || 0);
    const oppBid = side === "UP" ? (bids.down || 0) : (bids.up  || 0);
    if (!side || myBid <= 0) {
      if (side && oppBid >= 0.85) return { pct: 5,  label: "reversao total",  color: "red" };
      if (side && oppBid > 0)     return { pct: 15, label: "livro EL zerou",  color: "red" };
      return null;
    }

    // Opp bid dominante — limiar 0.80 confirmado em 1368 slugs reais
    // opp>=0.80: EL 6% | opp 0.70-0.80: EL 78% (zona de risco, monitorar)
    if (secs === null || secs > 35) {
      if (oppBid >= 0.80)
        return { pct: 6,  label: "opp>=0.80: reversao dominante (94% rev real)", color: "red" };
      if (oppBid >= 0.70)
        return { pct: 78, label: "opp 0.70-0.80: zona de risco (78% EL real)",   color: "orange" };
    }

    if (secs !== null && secs <= 35) {
      if (oppBid >= 0.85) return { pct: 8,  label: "reversao secs<=35",      color: "red" };
      if (myBid  >= 0.85) return { pct: 94, label: "near win bid>=0.85",     color: "green" };
      if (myBid  >= 0.75) return { pct: 82, label: "secs<=35 bid 0.75-0.85", color: "yellow" };
    }

    if (secs !== null && secs >= 36 && secs <= 70 && myBid >= 0.88)
      return { pct: 91, label: "PP window bid>=0.88", color: "green" };

    if (myBid < 0.50 && oppBid > 0)
      return { pct: 22, label: "bid<0.50 hedge gap", color: "red" };

    // Zona morta — pos-FAK-removal: 20w/4s em bid>=0.80 = 83%
    if (myBid >= 0.80) return { pct: 83, label: "zona morta bid>=0.80",     color: "yellow" };
    if (myBid >= 0.73) return { pct: 73, label: "zona morta bid 0.73-0.80", color: "yellow" };
    if (myBid >= 0.65) return { pct: 55, label: "zona morta bid 0.65-0.73", color: "orange" };
    if (myBid >= 0.50) return { pct: 38, label: "zona morta bid 0.50-0.65", color: "red" };
    return null;
  }

  // ---------------------------------------------------------------------------
  // Probabilidade de inversao — perspectiva dupla (posicao atual vs novo lider)
  // Calibrado em 585 slugs COM inversao detectada — 1368 slugs reais (sec 24.4)
  // ---------------------------------------------------------------------------
  function calcWinProbInversion(invBid, invStrong, secs) {
    // Retorna { el: {pct, label, color}, reversal: {pct, label, color} }
    let elPct, revPct, label;

    if (invStrong) {
      // bid >= 0.72 (campo inversion_strong): 22% EL, 78% reversal (real n=181)
      elPct  = 22;
      revPct = 78;
      label  = `inversao forte bid=${fmt(invBid)}`;
    } else if (invBid >= 0.68) {
      // bid 0.68-0.72: 71% reversal (real n=73)
      elPct  = 29;
      revPct = 71;
      label  = `inversao solida bid=${fmt(invBid)}`;
    } else if (invBid >= 0.62) {
      // bid 0.62-0.68: 65% reversal (real n=110)
      elPct  = 35;
      revPct = 65;
      label  = `inversao media bid=${fmt(invBid)}`;
    } else {
      // bid 0.55-0.62: 57% reversal (real n=237)
      elPct  = 43;
      revPct = 57;
      label  = `inversao inicial bid=${fmt(invBid)}`;
    }

    if (secs !== null && secs <= 60) {
      // Perto do fim: inversao ja estabelecida tem menos tempo para reverter
      // boost conservador de ~5pp na direção da reversão
      revPct = Math.min(95, revPct + 5);
      elPct  = 100 - revPct;
      label  = label + ` secs<=${secs}`;
    }

    return {
      el:      { pct: elPct,  label: `lado atual: ${label}`,    color: "red" },
      reversal:{ pct: revPct, label: `novo lider: ${label}`,    color: "green" },
    };
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
          detail: `livro EL zerou, opp=${oppBid.toFixed(3)} -- hedge`, urgent: true };
      return { action: "SEM DADOS", color: "gray", detail: "aguardando bids" };
    }

    if (myBid < HEDGE_THR && oppBid > 0)
      return { action: "STOP URGENTE", color: "red",
        detail: `bid=${myBid.toFixed(3)} < 0.50 -- sair` };

    if (secs !== null && secs <= WIN_SECS) {
      if (oppBid >= WIN_BID)
        return { action: "SAIDA URGENTE", color: "red",
          detail: `reversao: ${side === "UP" ? "DOWN" : "UP"}=${oppBid.toFixed(3)} >= ${WIN_BID}` };
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
      return { action: myBid < 0.70 ? "ZONA MORTA" : "SEM PROTECAO",
        color: myBid < 0.70 ? "yellow" : "gray",
        detail: `bid=${myBid.toFixed(3)} | ${ppNote}` };

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
    const barColor = { green:"#22c55e", yellow:"#eab308", orange:"#f97316", red:"#ef4444" }[wp.color] || "#64748b";
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
  // Secao automatica: usa o lado do Early Leader como posicao assumida.
  // Reseta sozinha quando o slug muda (novo mercado 5min).
  // ---------------------------------------------------------------------------
  function inversionDualHtml(invBid, invStrong, invSide, secs) {
    const { el, reversal } = calcWinProbInversion(invBid, invStrong, secs);
    const barColor = { green: "#22c55e", yellow: "#eab308", orange: "#f97316", red: "#ef4444" };
    const elBar   = barColor[el.color]      || "#64748b";
    const revBar  = barColor[reversal.color] || "#64748b";
    return `<div class="elm-section elm-inversion-dual">
      <div class="elm-section-title">Inversao: ${esc(invSide)} ${invStrong ? "(FORTE)" : "(moderada)"}</div>
      <div class="elm-inversion-row">
        <span class="elm-inv-label">Posicao atual</span>
        <span class="elm-inv-pct" style="color:${elBar}">${el.pct}%</span>
      </div>
      <div class="elm-win-prob-bar-bg" style="margin-bottom:4px">
        <div class="elm-win-prob-bar" style="width:${el.pct}%;background:${elBar}"></div>
      </div>
      <div class="elm-inversion-row">
        <span class="elm-inv-label">Novo lider (${esc(invSide)})</span>
        <span class="elm-inv-pct" style="color:${revBar}">${reversal.pct}%</span>
      </div>
      <div class="elm-win-prob-bar-bg" style="margin-bottom:4px">
        <div class="elm-win-prob-bar" style="width:${reversal.pct}%;background:${revBar}"></div>
      </div>
      <div class="elm-win-prob-basis">${esc(el.label)}</div>
    </div>`;
  }

  function autoPositionHtml(state) {
    const bids  = state.bids || {};
    const secs  = state.secs_to_end;
    const ee    = state.ee  || {};
    const el    = state.el  || {};
    const side  = ee.early_side || null;

    if (!side) {
      return `<div class="elm-section elm-auto-pos-section">
        <div class="elm-section-title">Probabilidade (leader)</div>
        <div class="elm-auto-waiting">aguardando Early Leader (janela 181-240s)...</div>
      </div>`;
    }

    const wp  = calcWinProb(side, bids, secs);
    const rec = exitRecommendation(side, bids, secs);
    const myBid  = side === "UP" ? (bids.up  || 0) : (bids.down || 0);
    const oppBid = side === "UP" ? (bids.down || 0) : (bids.up  || 0);

    const myBidCls  = myBid  >= 0.85 ? "ok" : myBid  < 0.50 ? "bad" : "";
    const oppBidCls = oppBid >= 0.70 ? "bad" : "";
    const ppVal = myBid > 0
      ? (myBid >= PP_BID ? `DISPONIVEL (${fmt(myBid)})` : `falta +${(PP_BID - myBid).toFixed(3)}`)
      : "-";

    // Inversao: mostra probabilidade dupla quando detectada pelo AR tracker
    const inverted     = el.inverted || false;
    const invBid       = el.inversion_bid || 0;
    const invStrong    = el.inversion_strong || false;
    const invSide      = el.inversion_side || "";
    const invHtml = (inverted && invSide && invSide !== side)
      ? inversionDualHtml(invBid, invStrong, invSide, secs)
      : "";

    return `<div class="elm-section elm-auto-pos-section">
      <div class="elm-section-title">Probabilidade (leader: ${esc(side)})</div>
      ${winProbHtml(wp)}
      <div class="elm-pos-exit-badge ${statusClass(rec.color)}">${esc(rec.action)}</div>
      <div class="elm-pos-detail">${esc(rec.detail)}</div>
      ${invHtml}
      ${elmRow("Bid " + side + " (leader)", fmt(myBid),  myBidCls)}
      ${elmRow("Bid oposto " + (inverted ? "[!]" : ""), fmt(oppBid), oppBidCls)}
      ${elmRow("PP (0.88, 36-70s)",        ppVal, myBid >= PP_BID ? "ok" : "")}
    </div>`;
  }

  // ---------------------------------------------------------------------------
  // Posição assumida — stop / hedge por variante
  // Assumido: usuário entrou no 1º sinal detectado para o slug atual.
  // EE: sem stop (FAK removido); hedge em 0.50.
  // AR: stop em entry−0.03 (3 ticks, stop_ticks=3 do runner real);
  //     saída imediata se bid <= 0.50 (mid_book_reversal).
  // ---------------------------------------------------------------------------
  function positionGuidanceHtml(pos, bids, secs) {
    if (!pos || !pos.side) return "";

    const side    = pos.side;
    const variant = pos.variant || "";
    const entry   = typeof pos.entry_price === "number" ? pos.entry_price : 0;
    const myBid   = side === "UP" ? (bids.up  || 0) : (bids.down || 0);
    const oppBid  = side === "UP" ? (bids.down || 0) : (bids.up  || 0);
    const stopLvl = entry > 0 ? Math.round((entry - 0.03) * 1000) / 1000 : 0;
    const isEE    = variant === "EE";

    let action, color, detail;

    // Livro EL zerou
    if (myBid <= 0 && oppBid >= 0.85) {
      action = "SAIDA URGENTE";  color = "red";
      detail = `livro EL zerou + opp=${fmt(oppBid)} — reversão total`;
    } else if (myBid <= 0 && oppBid > 0) {
      action = isEE ? "HEDGE URGENTE" : "SAIDA IMEDIATA"; color = "red";
      detail = `livro EL zerou, opp=${fmt(oppBid)} — ${isEE ? "hedge oposto" : "exit"}`;
    // bid cruzou 0.50 — mid_book
    } else if (myBid > 0 && myBid <= 0.50 && oppBid > 0) {
      action = isEE ? "HEDGE URGENTE" : "SAIDA IMEDIATA"; color = "red";
      detail = `bid=${fmt(myBid)} <= 0.50 — ${isEE ? "comprar lado oposto" : "mid_book reversal"}`;
    // EE: sem stop, lógica de win/reversão
    } else if (isEE) {
      if (secs !== null && secs <= 35 && myBid >= 0.85) {
        action = "WIN"; color = "green";
        detail = `bid=${fmt(myBid)} >= 0.85, secs=${secs}s — aguardar resolução`;
      } else if (secs !== null && secs <= 35 && oppBid >= 0.85) {
        action = "SAIDA URGENTE"; color = "red";
        detail = `reversão: opp=${fmt(oppBid)} >= 0.85, secs=${secs}s`;
      } else {
        action = "HOLD — sem stop"; color = "gray";
        detail = `EE: não usar stop; hedge só se bid < 0.50`;
      }
    // AR: stop em entry−3ticks
    } else if (stopLvl > 0 && myBid <= stopLvl) {
      action = "STOP"; color = "red";
      detail = `bid=${fmt(myBid)} <= ${fmt(stopLvl)} (entrada ${fmt(entry)} −3 ticks)`;
    } else if (variant === "extreme_99") {
      action = myBid >= 0.97 ? "WIN próximo" : "HOLD"; color = myBid >= 0.97 ? "green" : "gray";
      detail = `extreme_99 — min risco; stop se bid <= ${fmt(stopLvl)}`;
    } else {
      const gaining = entry > 0 && myBid > entry;
      action = myBid >= 0.97 ? "WIN próximo" : (gaining ? "HOLD — ganhando" : "HOLD");
      color  = myBid >= 0.97 ? "green" : gaining ? "yellow" : "gray";
      detail = `stop em ${fmt(stopLvl)} | bid atual ${fmt(myBid)}`;
    }

    const pnl      = entry > 0 && myBid > 0 ? myBid - entry : null;
    const pnlStr   = pnl !== null ? (pnl >= 0 ? `+${fmt(pnl)}` : fmt(pnl)) : "—";
    const pnlColor = pnl === null ? "#64748b" : pnl > 0 ? "#22c55e" : pnl < 0 ? "#ef4444" : "#64748b";

    return `<div class="elm-section elm-position-section">
      <div class="elm-section-title">Posição: ${esc(variant)} ${esc(side)} @ ${fmt(entry)}</div>
      <div class="elm-row" style="margin-bottom:4px">
        <span class="elm-row-label">P&amp;L/share</span>
        <span class="elm-row-val" style="color:${pnlColor}">${esc(pnlStr)}</span>
      </div>
      <div class="elm-pos-exit-badge ${statusClass(color)}">${esc(action)}</div>
      <div class="elm-pos-detail">${esc(detail)}</div>
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
    const ee     = state.ee  || {};
    const el     = state.el  || {};

    const elSide   = ee.early_side || null;
    const inverted = el.inverted;

    const velVal = ee.el_vel != null
      ? `${ee.el_vel >= 0 ? "+" : ""}${fmt(ee.el_vel)} (min:0.13)` : "-";
    const velCls  = ee.el_vel >= 0.13 ? "ok" : (ee.el_vel > 0 ? "warn" : "");
    const contVal = ee.f3_ok === true ? "sim" : ee.f3_ok === false ? "NAO" : "aguardando";
    const contCls = ee.f3_ok === true ? "ok" : ee.f3_ok === false ? "bad" : "";

    // invHtml integrado ao autoPositionHtml (probabilidade dupla)

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

      ${autoPositionHtml(state)}

      ${positionGuidanceHtml(state.position, bids, secs)}

      <div class="elm-section">
        <div class="elm-section-title">Early Leader EE</div>
        ${elmRow("Lado",         elSide ? elSide : "nao detectado", elSide ? "ok" : "")}
        ${elmRow("bid_240",      fmt(ee.el_bid_240))}
        ${elmRow("bid_180",      fmt(ee.el_bid_180))}
        ${elmRow("Velocidade",   velVal, velCls)}
        ${elmRow("Continuidade", contVal, contCls)}
        ${elmRow("Amostras",     `${ee.n_s240 || 0}@240 / ${ee.n_s180 || 0}@180`)}
      </div>

      ${criteriaHtml(state.criteria)}

      <div class="elm-actions">
        <button class="elm-btn-hide" id="${PANEL_ID}-hide">Fechar</button>
      </div>
    `;

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
