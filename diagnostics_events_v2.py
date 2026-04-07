from market.events_discovery_v2 import (
    fetch_active_events,
    extract_relevant_btc_markets_from_events,
    print_debug_event_samples,
)

print("[TEST] Starting event-based diagnostics v2...")

all_events = []
for offset in (0, 200, 400):
    batch = fetch_active_events(limit=200, offset=offset)
    if not batch:
        break
    all_events.extend(batch)

print(f"[TEST] Total active events loaded: {len(all_events)}")
print_debug_event_samples(all_events, limit=20)

btc_markets = extract_relevant_btc_markets_from_events(all_events)

print("\n[RESULT] Relevant BTC markets from active events:")
if not btc_markets:
    print("[RESULT] No BTC active/current-next markets found in scanned event pages.")
else:
    for item in btc_markets[:30]:
        print(
            f"- {item['market_question']} | endDate={item['endDate']} | secs_to_end={item['seconds_to_end']} | event_slug={item['event_slug']} | market_slug={item['market_slug']}"
        )

print("\n[TEST] Event-based diagnostics v2 finished 🚀")
