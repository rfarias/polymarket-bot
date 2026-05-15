(() => {
  "use strict";

  const STATE_URLS = ["http://127.0.0.1:8765/state", "http://localhost:8765/state"];
  const PANEL_ID = "pm-manual-assist-ext";
  const AUTO_FILL_ON_READY = false;
  let lastFilledWindowId = "";
  let dragState = null;
  let lastWorkingUrl = STATE_URLS[0];

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

  function statusMode(state) {
    const action = String(state.active_action || state.suggested_action || "").toLowerCase();
    const price = state.active_price ?? state.entry_price;
    const side = state.active_side || state.setup_side;
    if ((action.includes("comprar") || action.includes("limite")) && side && price !== null && price !== undefined) {
      return "PRONTO";
    }
    if (state.setup_reason === "invalid_book_both_sides_rich" || state.safety_label === "UNSAFE") {
      return "DESCARTAR";
    }
    return "AGUARDE";
  }

  function badgeColor(mode) {
    if (mode === "PRONTO") return "#1f9d55";
    if (mode === "DESCARTAR") return "#d64545";
    return "#6b7280";
  }

  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) return panel;
    panel = document.createElement("div");
    panel.id = PANEL_ID;
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
    dragState = { offsetX: event.clientX - rect.left, offsetY: event.clientY - rect.top };
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

  function detectInputs() {
    const inputs = findVisibleElements("input");
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
    if (side === "UP") return findButtonByText(["up", "buy up"]);
    if (side === "DOWN") return findButtonByText(["down", "buy down"]);
    return null;
  }

  function autofillTicket(state) {
    const side = state.active_side || state.setup_side;
    const price = state.active_price ?? state.entry_price;
    if (!state || !side || price === null || price === undefined) return false;
    const sideButton = chooseSide(side);
    const { priceInput, qtyInput } = detectInputs();
    if (!sideButton || !priceInput || !qtyInput) return false;
    sideButton.click();
    setNativeValue(priceInput, Number(price).toFixed(2));
    setNativeValue(qtyInput, state.default_qty || 6);
    lastFilledWindowId = state.window_id || "";
    return true;
  }

  function row(label, value) {
    return `<div class="pm-row"><span class="pm-label">${label}:</span> ${value}</div>`;
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
      <div class="pm-criteria">
        ${criteria
          .map((item) => {
            const ok = Boolean(item.ok);
            const cls = ok ? "pm-ok" : "pm-block";
            const detail = item.detail ? `<div class="pm-criterion-detail">${escapeHtml(item.detail)}</div>` : "";
            return `
              <div class="pm-criterion ${cls}">
                <div class="pm-criterion-main">
                  <span class="pm-dot"></span>
                  <span class="pm-criterion-label">${escapeHtml(item.label)}</span>
                  <span class="pm-criterion-value">${escapeHtml(item.value)}</span>
                </div>
                <div class="pm-criterion-accepted">${escapeHtml(item.accepted)}</div>
                ${detail}
              </div>
            `;
          })
          .join("")}
      </div>
    `;
  }

  function render(state) {
    const panel = ensurePanel();
    const mode = statusMode(state);
    const badge = badgeColor(mode);
    const activeSetup = "quase resolvido";
    const activeMarket = state.title || state.active_setup_market || "BTC current";
    const activePrice = state.entry_price ?? state.active_price;
    const side = state.setup_side || state.active_side || "";
    const criteria = Array.isArray(state.entry_criteria) ? state.entry_criteria : [];
    const criteriaReady = criteria.length > 0 && criteria.every((item) => Boolean(item.ok));
    const activeAction = criteriaReady ? "entrada liberada" : "aguardar";

    const headline =
      side && activePrice !== null && activePrice !== undefined
        ? `${activeSetup} | ${side} ${fmt(activePrice, 2)}`
        : `${activeSetup} | ${side || "sem lado"}`;

    const actionLine = `${activeMarket} | ${activeAction || "-"}${side ? ` ${side}` : ""}${activePrice !== null && activePrice !== undefined ? ` ${fmt(activePrice, 2)}` : ""}`;
    const message = [state.suggested_detail, state.status_note]
      .filter((part) => part && String(part).trim() && String(part).trim() !== "-")
      .join(" | ");
    const releaseText = criteriaReady ? "entrada liberada" : "entrada bloqueada";
    const releaseClass = criteriaReady ? "pm-release-ready" : "pm-release-wait";

    panel.innerHTML = `
      <div class="pm-head">
        <div class="pm-title">${headline}</div>
        <div class="pm-badge" style="background:${criteriaReady ? "#1f9d55" : badge}">${criteriaReady ? "LIBERADA" : mode}</div>
      </div>
      <div class="pm-release ${releaseClass}">${releaseText}</div>
      ${row("Acao", escapeHtml(actionLine))}
      ${criteriaRows(state)}
      ${row("Mensagem", escapeHtml(message || "-"))}
      <div class="pm-actions">
        <button class="pm-fill" id="${PANEL_ID}-fill">Preencher</button>
        <button class="pm-hide" id="${PANEL_ID}-hide">Ocultar</button>
      </div>
    `;

    panel.querySelector(`#${PANEL_ID}-fill`)?.addEventListener("click", () => autofillTicket(state));
    panel.querySelector(`#${PANEL_ID}-hide`)?.addEventListener("click", () => panel.remove());
  }

  async function fetchState() {
    if (typeof chrome !== "undefined" && chrome.runtime?.sendMessage) {
      try {
        const response = await chrome.runtime.sendMessage({ type: "pm_manual_assist_state" });
        if (response?.ok && response.state) return response.state;
        throw new Error(response?.error || "background_state_unavailable");
      } catch (err) {
        // Fall back to direct fetch below. This keeps the unpacked script usable
        // if the background worker is not loaded yet.
      }
    }
    const urls = [lastWorkingUrl, ...STATE_URLS.filter((url) => url !== lastWorkingUrl)];
    let lastError = null;
    for (const url of urls) {
      try {
        const res = await fetch(url, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        lastWorkingUrl = url;
        return await res.json();
      } catch (err) {
        lastError = err;
      }
    }
    throw lastError || new Error("state_unavailable");
  }

  async function tick() {
    try {
      const state = await fetchState();
      render(state);
      if (AUTO_FILL_ON_READY && state.one_shot_ready && state.window_id && state.window_id !== lastFilledWindowId) {
        autofillTicket(state);
      }
    } catch (err) {
      render({
        title: "Assistente Manual",
        setup_side: "",
        entry_price: null,
        secs_to_end: "-",
        suggested_detail: "sem conexao com http://127.0.0.1:8765/state",
        status_note: String(err?.message || err || "servidor local indisponivel"),
        entry_criteria: [
          {
            label: "Conexao com servidor local",
            value: "falhou",
            accepted: "background da extensao deve acessar /state",
            ok: false,
            detail: String(err?.message || err || "recarregue a extensao"),
          },
        ],
      });
    }
  }

  setInterval(tick, 250);
  tick();
})();
