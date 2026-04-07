from market.markets_discovery_v2 import (
    fetch_active_markets,
    print_debug_market_samples,
    extract_relevant_btc_markets,
)

print("[TEST] Starting markets-based diagnostics v2...")

all_markets = []
for offset in range(0, 2000, 100):
    batch = fetch_active_markets(limit=100, offset=offset)
    if not batch:
        break
    all_markets.extend(batch)

print(f"[TEST] Total active markets loaded: {len(all_markets)}")
print_debug_market_samples(all_markets, limit=25)

btc_markets = extract_relevant_btc_markets(all_markets)

print("\n[RESULT] Relevant BTC active/current-next markets:")
if not btc_markets:
    print("[RESULT] No BTC active/current-next markets found in scanned market pages.")
else:
    for item in btc_markets[:30]:
        print(
            f"- {item['question']} | endDate={item['endDate']} | secs_to_end={item['seconds_to_end']} | slug={item['slug']} | enableOrderBook={item['enableOrderBook']}"
        )

print("\n[TEST] Markets-based diagnostics v2 finished 🚀")
