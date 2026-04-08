from market.queue_5m import build_5m_queue
from market.slug_discovery import fetch_event_by_slug

print("[TEST] Starting 5m metadata diagnostics...")

queue = build_5m_queue()

for slot_name in ("current", "next_1", "next_2"):
    item = queue.get(slot_name)
    print(f"\n[{slot_name.upper()}]")
    if not item:
        print("none")
        continue

    slug = item["slug"]
    print(f"slug={slug}")
    print(f"title={item['title']}")
    print(f"seconds_to_end={item['seconds_to_end']}")

    event = fetch_event_by_slug(slug)
    if not event:
        print("[ERROR] Could not reload event by slug")
        continue

    print(f"event_keys={sorted(list(event.keys()))}")

    markets = event.get("markets") or []
    print(f"markets_count={len(markets)}")

    if not markets:
        continue

    market = markets[0]
    print(f"market_keys={sorted(list(market.keys()))}")

    interesting_fields = [
        "question",
        "slug",
        "active",
        "closed",
        "acceptingOrders",
        "enableOrderBook",
        "endDate",
        "startDate",
        "clobTokenIds",
        "outcomes",
        "groupItemTitle",
        "liquidityClob",
        "volumeClob",
    ]

    print("market_interesting_fields:")
    for field in interesting_fields:
        print(f"- {field}={market.get(field)}")

print("\n[TEST] 5m metadata diagnostics finished 🚀")
