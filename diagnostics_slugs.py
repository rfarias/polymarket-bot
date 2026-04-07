from market.slug_discovery import fetch_btc_fast_events

print("[TEST] Starting slug-based diagnostics...")

events = fetch_btc_fast_events()

print("\n[RESULT] BTC fast events by slug:")
if not events:
    print("[RESULT] No BTC fast events fetched by slug.")
else:
    for ev in events:
        title = ev.get("title")
        slug = ev.get("slug")
        end_date = ev.get("endDate")
        timeframe = ev.get("_derived_timeframe")
        markets = ev.get("markets") or []
        print(f"- title={title} | slug={slug} | timeframe={timeframe} | endDate={end_date} | markets={len(markets)}")

        for m in markets[:5]:
            print(
                "   ->",
                m.get("question"),
                "| slug=", m.get("slug"),
                "| active=", m.get("active"),
                "| closed=", m.get("closed"),
                "| acceptingOrders=", m.get("acceptingOrders"),
                "| enableOrderBook=", m.get("enableOrderBook"),
            )

print("\n[TEST] Slug-based diagnostics finished 🚀")
