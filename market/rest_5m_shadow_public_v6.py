import time

from market.rest_5m_shadow_public_v5 import (
    MIN_STABLE_SNAPSHOTS,
    POLL_INTERVAL_SECONDS,
    _allow_tradable_for_slot,
    _build_slot_bundle,
    _compute_display_metrics,
    _compute_executable_metrics,
    _current_secs_to_end,
    _fetch_slot_state,
    _print_slot_debug,
    _slot_snapshot,
)
from market.queue_5m_v5 import UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1
from market.setup1_policy import ARBITRAGE_SUM_ASKS_MAX, classify_signal
from market.setup1_broker_executor_v3 import Setup1BrokerExecutorV3
from market.dryrun_broker import DryRunBroker
from market.dryrun_lifecycle_v1 import advance_public_dryrun_lifecycle_v1


def monitor_setup1_shadow_public_rest_v6(duration_seconds: int = 60) -> None:
    broker = DryRunBroker()
    executor = Setup1BrokerExecutorV3(broker=broker, shadow_only=False)

    print("[BROKER_HEALTH]", broker.healthcheck().as_dict())
    slot_bundle = _build_slot_bundle()
    print("[QUEUE] 5m queue summary:")
    for slot_name in ("current", "next_1", "next_2"):
        item = slot_bundle["queue"].get(slot_name)
        if item:
            print(f"- {slot_name}: {item['seconds_to_end']}s | {item['title']} | slug={item['slug']}")
        else:
            print(f"- {slot_name}: none")

    display_stable_counts = {"current": 0, "next_1": 0, "next_2": 0}
    tradable_stable_counts = {"current": 0, "next_1": 0, "next_2": 0}
    started_at = time.time()
    next_print = 0.0

    while time.time() - started_at < duration_seconds:
        slot_state = _fetch_slot_state(slot_bundle)

        if time.time() >= next_print:
            next_print = time.time() + POLL_INTERVAL_SECONDS
            print("\n===== SETUP1 SHADOW PUBLIC REST SNAPSHOT V6 =====")
            next_1_item = slot_bundle["queue"].get("next_1")
            next_1_secs = _current_secs_to_end(next_1_item.get("seconds_to_end") if next_1_item else None, started_at)

            for slot_name in ("current", "next_1", "next_2"):
                item = slot_bundle["queue"].get(slot_name)
                secs = _current_secs_to_end(item.get("seconds_to_end") if item else None, started_at)
                snap = _slot_snapshot(slot_state, slot_name)
                display_metrics, display_reason = _compute_display_metrics(snap)
                executable_metrics, executable_reason = _compute_executable_metrics(snap)

                if display_metrics:
                    display_stable_counts[slot_name] += 1
                else:
                    display_stable_counts[slot_name] = 0
                display_signal = classify_signal(display_metrics, display_stable_counts[slot_name], MIN_STABLE_SNAPSHOTS)

                tradable_allowed, tradable_gate_reason = _allow_tradable_for_slot(slot_name, executor, next_1_secs)
                tradable_metrics = None
                if executable_metrics and executable_metrics["sum_asks"] <= ARBITRAGE_SUM_ASKS_MAX and tradable_allowed:
                    tradable_metrics = executable_metrics

                if tradable_metrics:
                    tradable_stable_counts[slot_name] += 1
                else:
                    tradable_stable_counts[slot_name] = 0
                tradable_signal = classify_signal(tradable_metrics, tradable_stable_counts[slot_name], MIN_STABLE_SNAPSHOTS)

                print(f"\n[{slot_name.upper()}] secs_to_end={secs} | display_stable={display_stable_counts[slot_name]} | tradable_stable={tradable_stable_counts[slot_name]} | display_signal={display_signal} | tradable_signal={tradable_signal}")
                if display_metrics:
                    print(f"DISPLAY asks={display_metrics['up_ask']}/{display_metrics['down_ask']} | bids={display_metrics['up_bid']}/{display_metrics['down_bid']} | sum_asks={display_metrics['sum_asks']} | sum_bids={display_metrics['sum_bids']}")
                else:
                    print(f"display_metrics=None | reason={display_reason}")
                if executable_metrics:
                    print(f"EXECUTABLE asks={executable_metrics['up_ask']}/{executable_metrics['down_ask']} | bids={executable_metrics['up_bid']}/{executable_metrics['down_bid']} | sum_asks={executable_metrics['sum_asks']} | sum_bids={executable_metrics['sum_bids']}")
                else:
                    print(f"executable_metrics=None | reason={executable_reason}")
                _print_slot_debug(slot_name, snap, display_reason, executable_reason, tradable_gate_reason)
                print(f"[{slot_name.upper()} DEBUG] display_signal={display_signal} | tradable_signal={tradable_signal}")

                if slot_name in ("next_1", "next_2") and item:
                    if tradable_metrics is not None:
                        logs = executor.process_market_tick(
                            slot_name=slot_name,
                            event_slug=item["slug"],
                            signal=tradable_signal,
                            metrics=tradable_metrics,
                            secs_to_end=secs,
                            deadline_trigger=UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1 if slot_name == "next_1" else None,
                        )
                        for line in logs:
                            print(line)
                    else:
                        if display_signal in ("watching", "armed"):
                            print(f"[DISPLAY_ONLY] {slot_name}: tradable confirmation not satisfied")

            lifecycle_logs = advance_public_dryrun_lifecycle_v1(executor, broker, slot_state)
            for line in lifecycle_logs:
                print(line)

            print("\n[EXECUTOR_SNAPSHOT]")
            print(executor.snapshot())
            print("\n[OPEN_ORDERS]")
            print([o.as_dict() for o in broker.get_open_orders()])

        time.sleep(POLL_INTERVAL_SECONDS)
