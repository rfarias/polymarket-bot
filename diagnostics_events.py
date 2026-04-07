from market.events_discovery import fetch_active_events, extract_relevant_btc_markets_from_events

print("[TEST] Starting event-based diagnostics...")

all_events = []
for offset in (0, 100, 200):
    batch = fetch_active_events(limit=100, offset=offset)
    if not batch:
        break
    all_events.extend(batch)

print(f"[TEST] Total active events loaded: {len(all_events)}")

btc_markets = extract_relevant_btc_markets_from_events(all_events)

print("\n[RESULT] Relevant BTC markets from active events:")
if not btc_markets:
    print("[RESULT] No BTC active/current-next markets found in scanned event pages.")
else:
    for item in btc_markets[:20]:
        print(
            f"- {item['market_question']} | endDate={item['endDate']} | secs_to_end={item['seconds_to_end']} | slug={item['market_slug']}"
        )

print("\n[TEST] Event-based diagnostics finished 🚀")
