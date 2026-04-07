from market.page_discovery import discover_btc_fast_market_links

print("[TEST] Starting page-based diagnostics...")

links = discover_btc_fast_market_links()

print("\n[RESULT] BTC fast market links:")
if not links:
    print("[RESULT] No BTC fast market links found.")
else:
    for link in links[:50]:
        print("-", link)

print("\n[TEST] Page-based diagnostics finished 🚀")
