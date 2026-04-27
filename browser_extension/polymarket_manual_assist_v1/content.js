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

  function render(state) {
    const panel = ensurePanel();
    const mode = statusMode(state);
    const badge = badgeColor(mode);
    const activeSetup = state.active_setup_name || "sem setup";
    const activeMarket = state.active_setup_market || state.title || "BTC current";
    const activeAction = state.active_action || state.suggested_action || "-";
    const activePrice = state.active_price ?? state.entry_price;
    const side = state.active_side || state.setup_side || "";

    const headline =
      mode === "PRONTO" && side && activePrice !== null && activePrice !== undefined
        ? `${activeSetup} | ${activeAction} ${fmt(activePrice, 2)}`
        : `${activeSetup} | ${mode}`;

    const actionLine = `${activeMarket} | ${activeAction || "-"}${side ? ` ${side}` : ""}${activePrice !== null && activePrice !== undefined ? ` ${fmt(activePrice, 2)}` : ""}`;
    const message = [state.market_context, state.next_seconds_outlook, state.suggested_detail]
      .filter((part) => part && String(part).trim() && String(part).trim() !== "-")
      .join(" | ");

    panel.innerHTML = `
      <div class="pm-head">
        <div class="pm-title">${headline}</div>
        <div class="pm-badge" style="background:${badge}">${mode}</div>
      </div>
      ${row("Acao", actionLine)}
      ${row("Mensagem", message || state.status_note || "-")}
      <div class="pm-actions">
        <button class="pm-fill" id="${PANEL_ID}-fill">Preencher</button>
        <button class="pm-hide" id="${PANEL_ID}-hide">Ocultar</button>
      </div>
    `;

    panel.querySelector(`#${PANEL_ID}-fill`)?.addEventListener("click", () => autofillTicket(state));
    panel.querySelector(`#${PANEL_ID}-hide`)?.addEventListener("click", () => panel.remove());
  }

  async function fetchState() {
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
        active_setup_name: "sem setup",
        active_setup_market: "BTC current",
        active_action: "aguardar",
        active_side: "",
        active_price: null,
        market_context: "servidor local indisponivel",
        next_seconds_outlook: "sem leitura curta disponivel",
        secs_to_end: "-",
        suggested_detail: "inicie python run_manual_signal_server_v1.py --qty 6",
      });
    }
  }

  setInterval(tick, 250);
  tick();
})();
