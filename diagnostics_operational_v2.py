from market.slug_discovery_v2 import fetch_operational_fast_events
from market.slug_discovery_v3 import split_current_and_next

print("[TEST] Starting operational diagnostics v2...")

events = fetch_operational_fast_events()

print("\n[RESULT] Operational fast events:")
if not events:
    print("[RESULT] No operational fast events found.")
else:
    for ev in events:
        print(
            f"- tf={ev['timeframe']} | secs_to_end={ev['seconds_to_end']} | title={ev['title']} | event_slug={ev['slug']} | market_slug={ev['market_slug']}"
        )

slots = split_current_and_next(events)

print("\n[RESULT] Current and next by timeframe (refined):")
for tf, pair in slots.items():
    print(f"\n[{tf.upper()}]")
    current_ev = pair.get("current")
    next_ev = pair.get("next")

    if current_ev:
        print(
            f"current -> secs_to_end={current_ev['seconds_to_end']} | title={current_ev['title']} | event_slug={current_ev['slug']}"
        )
    else:
        print("current -> none")

    if next_ev:
        print(
            f"next    -> secs_to_end={next_ev['seconds_to_end']} | title={next_ev['title']} | event_slug={next_ev['slug']}"
        )
    else:
        print("next    -> none")

print("\n[TEST] Operational diagnostics v2 finished 🚀")
