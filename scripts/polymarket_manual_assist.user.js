// ==UserScript==
// @name         Polymarket Manual Assist V1
// @namespace    polymarket-bot
// @version      1.0
// @description  Overlay + ticket autofill for manual almost-resolved entries
// @match        https://polymarket.com/*
// @grant        none
// ==/UserScript==

(function () {
  "use strict";

  const STATE_URL = "http://127.0.0.1:8765/state";
  const AUTO_FILL_ON_READY = true;
  const PANEL_ID = "pm-manual-assist-v1";
  const DEFAULT_Y_OFFSET = 86;
  let lastFilledWindowId = "";
  let dragState = null;

  function fmt(value, digits = 2, suffix = "") {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
    return `${Number(value).toFixed(digits)}${suffix}`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function panelColor(label) {
    if (label === "SAFE") return "#1f9d55";
    if (label === "CAUTION") return "#d69e2e";
    if (label === "UNSAFE") return "#d64545";
    return "#6b7280";
  }

  function findVisibleElements(selector) {
    return Array.from(document.querySelectorAll(selector)).filter((el) => {
      const r = el.getBoundingClientRect();
      const s = window.getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
    });
  }

  function findButtonByText(patterns) {
    const buttons = findVisibleElements("button,[role='button']");
    for (const button of buttons) {
      const text = (button.textContent || "").trim().toLowerCase();
      if (patterns.some((p) => text === p || text.includes(p))) return button;
    }
    return null;
  }

  function setNativeValue(input, value) {
    const descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
    descriptor?.set?.call(input, String(value));
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function pickTradePanel() {
    const tradeButton = findButtonByText(["trade"]);
    return tradeButton ? tradeButton.closest("div") : null;
  }

  function detectInputs() {
    const scope = pickTradePanel() || document.body;
    const inputs = Array.from(scope.querySelectorAll("input")).filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    });

    let priceInput = null;
    let qtyInput = null;
    for (const input of inputs) {
      const aria = `${input.getAttribute("aria-label") || ""} ${input.getAttribute("placeholder") || ""}`.toLowerCase();
      if (!priceInput && (aria.includes("price") || aria.includes("limit"))) priceInput = input;
      if (!qtyInput && (aria.includes("amount") || aria.includes("share") || aria.includes("qty") || aria.includes("quantity"))) qtyInput = input;
    }

    if (!qtyInput && inputs.length >= 1) qtyInput = inputs[inputs.length - 1];
    if (!priceInput && inputs.length >= 2) priceInput = inputs[0];
    return { priceInput, qtyInput };
  }

  function chooseSide(side) {
    if (side === "UP") {
      return findButtonByText(["up", "buy up"]);
    }
    if (side === "DOWN") {
      return findButtonByText(["down", "buy down"]);
    }
    return null;
  }

  function autofillTicket(state) {
    if (!state || !state.one_shot_ready || !state.setup_side || !state.entry_price) return false;
    const sideButton = chooseSide(state.setup_side);
    const { priceInput, qtyInput } = detectInputs();
    if (!sideButton || !priceInput || !qtyInput) return false;

    sideButton.click();
    setNativeValue(priceInput, Number(state.entry_price).toFixed(2));
    setNativeValue(qtyInput, state.default_qty || 6);
    lastFilledWindowId = state.window_id || "";
    return true;
  }

  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) return panel;
    panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.style.position = "fixed";
    panel.style.top = `${DEFAULT_Y_OFFSET}px`;
    panel.style.left = "50%";
    panel.style.transform = "translateX(-50%)";
    panel.style.zIndex = "999999";
    panel.style.width = "420px";
    panel.style.minWidth = "220px";
    panel.style.maxWidth = "min(92vw, 620px)";
    panel.style.minHeight = "180px";
    panel.style.maxHeight = "82vh";
    panel.style.background = "rgba(16,20,24,0.94)";
    panel.style.color = "#ecf1f7";
    panel.style.borderRadius = "10px";
    panel.style.padding = "10px 12px";
    panel.style.fontFamily = "Consolas, monospace";
    panel.style.boxShadow = "0 8px 30px rgba(0,0,0,0.35)";
    panel.style.pointerEvents = "auto";
    panel.style.cursor = "move";
    panel.style.resize = "both";
    panel.style.overflow = "auto";
    document.body.appendChild(panel);
    panel.addEventListener("mousedown", startDrag);
    window.addEventListener("mousemove", onDrag);
    window.addEventListener("mouseup", stopDrag);
    return panel;
  }

  function startDrag(event) {
    const panel = document.getElementById(PANEL_ID);
    if (!panel) return;
    if (event.target instanceof HTMLButtonElement) return;
    const rect = panel.getBoundingClientRect();
    if (event.clientX > rect.right - 18 && event.clientY > rect.bottom - 18) return;
    dragState = {
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
    };
    panel.style.transform = "none";
  }

  function onDrag(event) {
    const panel = document.getElementById(PANEL_ID);
    if (!panel || !dragState) return;
    panel.style.left = `${Math.max(0, event.clientX - dragState.offsetX)}px`;
    panel.style.top = `${Math.max(0, event.clientY - dragState.offsetY)}px`;
  }

  function stopDrag() {
    dragState = null;
  }

  function classifyState(state) {
    if (state.one_shot_ready && state.setup_side && state.entry_price) {
      return "READY";
    }
    if (state.setup_reason === "invalid_book_both_sides_rich" || state.safety_label === "UNSAFE") {
      return "DISCARD";
    }
    return "WAIT";
  }

  function inOperationalWindow(state) {
    const secs = Number(state.secs_to_end);
    if (Number.isNaN(secs)) return false;
    return secs <= 80 && secs >= 15;
  }

  function hasValue(value) {
    return value !== null && value !== undefined && value !== "" && !Number.isNaN(Number(value));
  }

  function row(label, value) {
    return `<div style="margin-top:4px;font-size:12px;color:#98a7b8;"><span style="color:#cbd5e1;">${label}:</span> ${value}</div>`;
  }

  function criteriaRows(state) {
    const criteria = Array.isArray(state.entry_criteria) && state.entry_criteria.length
      ? state.entry_criteria
      : [
          {
            label: "Servidor de criterios",
            value: "sem dados",
            accepted: "aguardando /state com entry_criteria",
            ok: false,
            detail: state.status_note || "reinicie o servidor manual se persistir",
          },
        ];
    return `
      <div style="margin-top:8px;display:grid;gap:5px;">
        ${criteria
          .map((item) => {
            const ok = Boolean(item.ok);
            const bg = ok ? "rgba(31,157,85,0.14)" : "rgba(214,69,69,0.13)";
            const border = ok ? "rgba(31,157,85,0.45)" : "rgba(214,69,69,0.45)";
            const dot = ok ? "#1f9d55" : "#d64545";
            const detail = item.detail
              ? `<div style="margin-left:16px;margin-top:2px;font-size:10px;color:#98a7b8;">${escapeHtml(item.detail)}</div>`
              : "";
            return `
              <div style="padding:5px 7px;border-radius:6px;background:${bg};border:1px solid ${border};">
                <div style="display:grid;grid-template-columns:10px minmax(0,1fr);align-items:center;gap:6px;font-size:11px;">
                  <span style="width:7px;height:7px;border-radius:999px;background:${dot};display:inline-block;"></span>
                  <span style="color:#ecf1f7;font-weight:700;min-width:0;overflow-wrap:anywhere;">${escapeHtml(item.label)}</span>
                  <span style="grid-column:2;color:#ecf1f7;text-align:left;white-space:normal;overflow-wrap:anywhere;font-size:10px;">${escapeHtml(item.value)}</span>
                </div>
                <div style="margin-left:16px;margin-top:2px;font-size:10px;color:#98a7b8;overflow-wrap:anywhere;">${escapeHtml(item.accepted)}</div>
                ${detail}
              </div>
            `;
          })
          .join("")}
      </div>
    `;
  }

  function sideArrow(side) {
    if (side === "UP") return "↑";
    if (side === "DOWN") return "↓";
    return "→";
  }

  function render(state) {
    const panel = ensurePanel();
    const mode = classifyState(state);
    const badge = mode === "READY" ? "#1f9d55" : mode === "DISCARD" ? "#d64545" : "#6b7280";
    const inWindow = inOperationalWindow(state);
    const criteria = Array.isArray(state.entry_criteria) ? state.entry_criteria : [];
    const criteriaReady = criteria.length > 0 && criteria.every((item) => Boolean(item.ok));
    const headline =
      criteriaReady
        ? `READY ${state.setup_side} ${fmt(state.entry_price, 2)} x ${state.default_qty || 6}`
        : mode === "DISCARD"
          ? "DISCARD"
          : `QUASE RESOLVIDO | ${state.setup_side || "sem lado"}`;
    const secondary =
      mode === "READY"
        ? `edge active | reason=${state.setup_reason || "-"}`
        : inWindow
          ? `inside window | no edge yet | reason=${state.setup_reason || "-"}`
          : `outside window | reason=${state.setup_reason || "-"}`
    ;
    const lines = [];
    lines.push(
      `<div style="margin-top:8px;padding:6px 8px;border-radius:6px;text-align:center;text-transform:uppercase;font-size:13px;font-weight:700;background:${
        criteriaReady ? "rgba(31,157,85,0.22)" : "rgba(214,69,69,0.14)"
      };color:${criteriaReady ? "#8ff0b5" : "#ffb4b4"};border:1px solid ${criteriaReady ? "rgba(31,157,85,0.6)" : "rgba(214,69,69,0.45)"};">${
        criteriaReady ? "entrada liberada" : "entrada bloqueada"
      }</div>`
    );
    lines.push(row("Status", `${mode} | secs=${state.secs_to_end ?? "-"} | score=${state.manual_score ?? 0}`));
    lines.push(row("Action", `${escapeHtml(state.suggested_action || "-")} | ${escapeHtml(state.suggested_detail || "-")}`));
    lines.push(criteriaRows(state));
    if (hasValue(state.watch_window_eta_secs) && Number(state.watch_window_eta_secs) > 0) {
      lines.push(row("Watch In", fmt(state.watch_window_eta_secs, 0, "s")));
    }
    if (hasValue(state.reaction_deadline_secs)) {
      lines.push(row("Reaction", fmt(state.reaction_deadline_secs, 0, "s")));
    }
    if (state.trend_label) {
      lines.push(row("Trend", state.trend_label));
    }
    if (state.reversal_risk) {
      lines.push(row("Reversal", state.reversal_risk));
    }
    if (hasValue(state.price_to_beat_bps)) {
      lines.push(row("Price To Beat", `${sideArrow(state.price_to_beat_side)} ${fmt(state.price_to_beat_bps, 2, "bps")}`));
    }
    if (hasValue(state.buffer_bps)) {
      lines.push(row("Buffer", fmt(state.buffer_bps, 2, "bps")));
    }
    panel.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
        <div style="font-size:14px;font-weight:700;">${headline}</div>
        <div style="background:${criteriaReady ? "#1f9d55" : badge};padding:4px 8px;border-radius:6px;font-weight:700;">${criteriaReady ? "LIBERADA" : mode}</div>
      </div>
      <div style="margin-top:8px;">${lines.join("")}</div>
      <div style="margin-top:8px;display:flex;gap:8px;">
        <button id="${PANEL_ID}-fill" style="padding:6px 10px;border:0;border-radius:6px;background:#ecf1f7;color:#101418;cursor:pointer;">Fill Ticket</button>
        <button id="${PANEL_ID}-hide" style="padding:6px 10px;border:0;border-radius:6px;background:#26313b;color:#ecf1f7;cursor:pointer;">Hide</button>
      </div>
    `;

    panel.querySelector(`#${PANEL_ID}-fill`)?.addEventListener("click", () => autofillTicket(state));
    panel.querySelector(`#${PANEL_ID}-hide`)?.addEventListener("click", () => panel.remove());
  }

  async function tick() {
    try {
      const res = await fetch(STATE_URL, { cache: "no-store" });
      const state = await res.json();
      render(state);
      if (AUTO_FILL_ON_READY && state.one_shot_ready && state.window_id && state.window_id !== lastFilledWindowId) {
        autofillTicket(state);
      }
    } catch (err) {
      render({
        title: "Manual Assist",
        secs_to_end: "-",
        safety_label: "BLOCKED",
        manual_score: 0,
        reaction_deadline_secs: null,
        setup_side: "",
        entry_price: null,
        default_qty: 6,
        trend_label: "-",
        reversal_risk: "-",
        price_to_beat_bps: null,
        buffer_bps: null,
        leader_edge: null,
        status_note: "Local signal server unavailable. Start python run_manual_signal_server_v1.py",
      });
    }
  }

  setInterval(tick, 100);
  tick();
})();
