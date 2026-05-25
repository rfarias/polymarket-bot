import json, datetime

LOG = "logs/current_almost_resolved_real_20260521_125445/current_almost_resolved_real.jsonl"

OPEN_SLUGS = {
    "btc-updown-5m-1779378900",
    "btc-updown-5m-1779380100",
    "btc-updown-5m-1779381000",
    "btc-updown-5m-1779381300",
    "btc-updown-5m-1779383400",
    "btc-updown-5m-1779386100",
    "btc-updown-5m-1779388800",
    "btc-updown-5m-1779389100",
    "btc-updown-5m-1779389400",
    "btc-updown-5m-1779390300",
    "btc-updown-5m-1779394200",
}

RELEVANT = {
    "enter", "fill", "fill_on_invalid_signal", "entry_cancel",
    "flat", "redeem_flat", "awaiting_redeem", "exit_posted",
    "external_close_detected",
}

with open(LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get("type", "")
        if t not in RELEVANT:
            continue
        trade = e.get("trade") or {}
        slug = (
            trade.get("event_slug")
            or (e.get("signal") or {}).get("event_slug")
            or e.get("slug")
            or ""
        )
        if slug not in OPEN_SLUGS:
            continue
        ts = datetime.datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S")
        qty_f = trade.get("entry_qty_filled", 0)
        reason = (e.get("reason") or trade.get("last_reason") or "")[:50]
        print(f"{slug[-18:]} | {ts} | {t:<30} | qty_f={qty_f:.1f} | {reason}")
