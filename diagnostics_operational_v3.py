from market.slug_discovery_v2 import fetch_operational_fast_events
from market.operational_slots_v2 import build_operational_slots, FIVE_MINUTE_UNFILLED_EXIT_TRIGGER_SECS_TO_END

print("[TEST] Starting operational diagnostics v3...")

events = fetch_operational_fast_events()

print("\n[RESULT] Operational fast events:")
if not events:
    print("[RESULT] No operational fast events found.")
else:
    for ev in events:
        print(
            f"- tf={ev['timeframe']} | secs_to_end={ev['seconds_to_end']} | title={ev['title']} | event_slug={ev['slug']} | market_slug={ev['market_slug']}"
        )

slots = build_operational_slots(events)

print("\n[RESULT] Timeframe slots:")
for tf, data in slots.items():
    print(f"\n[{tf.upper()}]")
    if tf == "5m":
        current_ev = data.get("current")
        next_1 = data.get("next_1")
        next_2 = data.get("next_2")

        print(
            f"current -> {current_ev['seconds_to_end']}s | {current_ev['title']}" if current_ev else "current -> none"
        )
        print(
            f"next_1  -> {next_1['seconds_to_end']}s | {next_1['title']}" if next_1 else "next_1  -> none"
        )
        print(
            f"next_2  -> {next_2['seconds_to_end']}s | {next_2['title']}" if next_2 else "next_2  -> none"
        )
        print(
            f"rule -> cancel unfinished 2-leg orders on next_1 when secs_to_end <= {FIVE_MINUTE_UNFILLED_EXIT_TRIGGER_SECS_TO_END}"
        )
    else:
        current_ev = data.get("current")
        next_ev = data.get("next")
        print(
            f"current -> {current_ev['seconds_to_end']}s | {current_ev['title']}" if current_ev else "current -> none"
        )
        print(
            f"next    -> {next_ev['seconds_to_end']}s | {next_ev['title']}" if next_ev else "next    -> none"
        )

print("\n[TEST] Operational diagnostics v3 finished 🚀")
