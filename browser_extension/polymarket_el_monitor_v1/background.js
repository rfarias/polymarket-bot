const STATE_URLS = ["http://127.0.0.1:8766/state", "http://localhost:8766/state"];
let lastWorkingUrl = STATE_URLS[0];

async function fetchState() {
  const urls = [lastWorkingUrl, ...STATE_URLS.filter((u) => u !== lastWorkingUrl)];
  let lastError = null;
  for (const url of urls) {
    try {
      const res = await fetch(url, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      lastWorkingUrl = url;
      return { ok: true, url, state: await res.json() };
    } catch (err) {
      lastError = err;
    }
  }
  return { ok: false, error: lastError ? String(lastError.message || lastError) : "unavailable" };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "pm_el_monitor_state") return false;
  fetchState().then(sendResponse);
  return true;
});
