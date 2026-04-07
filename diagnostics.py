from market.discovery import fetch_markets, filter_btc_markets

print("[TEST] Starting diagnostics...")

markets = fetch_markets()

if not markets:
    print("[TEST] No markets fetched. Check API.")
    exit()

btc_markets = filter_btc_markets(markets)

print("\n[RESULT] Sample BTC markets:")

for m in btc_markets[:5]:
    print("-", m.get("question"))

print("\n[TEST] Diagnostics finished successfully 🚀")
