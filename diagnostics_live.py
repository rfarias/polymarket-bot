from market.discovery_live import fetch_markets, filter_relevant_btc_markets

print("[TEST] Starting live diagnostics...")

markets = fetch_markets(limit=1000)

if not markets:
    print("[TEST] No markets fetched. Check API or internet connection.")
    raise SystemExit(1)

btc_markets = filter_relevant_btc_markets(markets)

print("\n[RESULT] Relevant BTC markets (current + next windows):")

if not btc_markets:
    print("[RESULT] No current/next BTC markets found in this fetch window.")
else:
    for m in btc_markets[:15]:
        print(
            "-",
            m.get("question"),
            "| endDate=",
            m.get("endDate"),
            "| active=",
            m.get("active"),
            "| closed=",
            m.get("closed")
        )

print("\n[TEST] Live diagnostics finished 🚀")
