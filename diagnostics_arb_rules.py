from market.slug_discovery_v2 import fetch_operational_fast_events
from market.operational_slots_v2 import build_operational_slots
from strategies.preopen_arb_rules import ArbConfig, ArbSlot, ArbSnapshot, decide_preopen_arb

print("[TEST] Starting pre-open arbitrage rules diagnostics...")

# Descoberta operacional
all_events = fetch_operational_fast_events()
slots = build_operational_slots(all_events)
five = slots["5m"]
next_1 = five.get("next_1")
next_2 = five.get("next_2")

if not next_1:
    print("[RESULT] No 5m next_1 available for arbitrage rules diagnostics.")
    raise SystemExit(0)

cfg = ArbConfig()
slot = ArbSlot(
    title=next_1["title"],
    slug=next_1["slug"],
    timeframe=next_1["timeframe"],
    seconds_to_end=next_1["seconds_to_end"],
)

print("\n[CONTEXT] 5m queue:")
print(f"next_1 -> secs_to_end={next_1['seconds_to_end']} | secs_to_open={slot.seconds_to_open} | title={next_1['title']}")
if next_2:
    print(f"next_2 -> secs_to_end={next_2['seconds_to_end']} | title={next_2['title']}")
else:
    print("next_2 -> none")

# Cenários de teste
scenarios = [
    (
        "no_fills_before_45s",
        ArbSnapshot(
            slot=ArbSlot(slot.title, slot.slug, slot.timeframe, seconds_to_end=345),
            yes_filled_qty=0,
            no_filled_qty=0,
            next2_has_liquidity=bool(next_2),
        ),
    ),
    (
        "one_leg_before_30s",
        ArbSnapshot(
            slot=ArbSlot(slot.title, slot.slug, slot.timeframe, seconds_to_end=338),
            yes_filled_qty=10,
            no_filled_qty=0,
            entry_price_open_leg=48,
            current_executable_exit_price=46,
            next2_has_liquidity=bool(next_2),
        ),
    ),
    (
        "one_leg_final_30s_in_profit",
        ArbSnapshot(
            slot=ArbSlot(slot.title, slot.slug, slot.timeframe, seconds_to_end=329),
            yes_filled_qty=10,
            no_filled_qty=0,
            entry_price_open_leg=48,
            current_executable_exit_price=49,
            next2_has_liquidity=bool(next_2),
        ),
    ),
    (
        "one_leg_final_30s_in_loss",
        ArbSnapshot(
            slot=ArbSlot(slot.title, slot.slug, slot.timeframe, seconds_to_end=329),
            yes_filled_qty=10,
            no_filled_qty=0,
            entry_price_open_leg=48,
            current_executable_exit_price=46,
            next2_has_liquidity=bool(next_2),
        ),
    ),
    (
        "scratch_wait_after_open",
        ArbSnapshot(
            slot=ArbSlot(slot.title, slot.slug, slot.timeframe, seconds_to_end=298),
            yes_filled_qty=10,
            no_filled_qty=0,
            other_order_cancelled=True,
            exit_order_live=True,
            seconds_since_open=3,
            entry_price_open_leg=48,
            current_executable_exit_price=47,
            next2_has_liquidity=bool(next_2),
        ),
    ),
    (
        "stop_after_open",
        ArbSnapshot(
            slot=ArbSlot(slot.title, slot.slug, slot.timeframe, seconds_to_end=294),
            yes_filled_qty=10,
            no_filled_qty=0,
            other_order_cancelled=True,
            exit_order_live=True,
            seconds_since_open=7,
            entry_price_open_leg=48,
            current_executable_exit_price=44,
            next2_has_liquidity=bool(next_2),
        ),
    ),
    (
        "locked_arb",
        ArbSnapshot(
            slot=ArbSlot(slot.title, slot.slug, slot.timeframe, seconds_to_end=320),
            yes_filled_qty=10,
            no_filled_qty=10,
            next2_has_liquidity=bool(next_2),
        ),
    ),
]

print("\n[RESULT] Rule decisions:")
for name, snapshot in scenarios:
    decision = decide_preopen_arb(snapshot, cfg)
    print(f"- {name}: action={decision.action} | reason={decision.reason}")

print("\n[TEST] Pre-open arbitrage rules diagnostics finished 🚀")
