import time

from market.live_guarded_config import load_live_guarded_config
from market.polymarket_broker_v2 import PolymarketBrokerV2
from market.rest_5m_shadow_public_v5 import (
    MIN_STABLE_SNAPSHOTS,
    _allow_tradable_for_slot,
    _build_slot_bundle,
    _compute_display_metrics,
    _compute_executable_metrics,
    _current_secs_to_end,
    _fetch_slot_state,
    _print_slot_debug,
    _slot_snapshot,
)
from market.setup1_policy import ARBITRAGE_SUM_ASKS_MAX, classify_signal
from market.setup1_broker_executor_v3 import Setup1BrokerExecutorV3


def monitor_live_minimal_guarded_v1(duration_seconds: int | None = None) -> None:
    cfg = load_live_guarded_config()
    print("[LIVE_GUARDED_CONFIG]", cfg.as_dict())

    if not cfg.enabled:
        print("[GUARD] Disabled. Set POLY_GUARDED_ENABLED=true to arm this runner.")
        return

    if not cfg.shadow_only and not cfg.real_posts_enabled:
        print("[GUARD] Refusing to run because real posts are disabled by config.")
        return

    if not cfg.shadow_only and cfg.real_posts_enabled:
        print("[GUARD] Real posting is intentionally blocked in v1 until live fill reconciliation + real flatten are implemented.")
        return

    broker = PolymarketBrokerV2.from_env()
    executor = Setup1BrokerExecutorV3(
        broker=broker,
        shadow_only=True,
        min_shares_per_leg=cfg.min_shares_per_leg,
    )

    health = broker.healthcheck()
    print("[BROKER_HEALTH]", health.as_dict())
    if not health.ok:
        print("[GUARD] Broker healthcheck failed; aborting guarded runner.")
        return

    run_for = duration_seconds or cfg.run_seconds
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

    while time.time() - started_at < run_for:
        slot_state = _fetch_slot_state(slot_bundle)

        if time.time() >= next_print:
            next_print = time.time() + 2.0
            print("\n===== LIVE MINIMAL GUARDED V1 SNAPSHOT =====")
            next_1_item = slot_bundle["queue"].get("next_1")
            next_1_secs = _current_secs_to_end(next_1_item.get("seconds_to_end") if next_1_item else None, started_at)
            active_count = sum(1 for s in executor.slots.values() if s.active_plan_id)

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
                if slot_name == "next_2" and not cfg.allow_next_2:
                    tradable_allowed = False
                    tradable_gate_reason = "next_2_disabled_by_guard"
                if active_count >= cfg.max_active_plans and executor.slots.get(slot_name) and executor.slots[slot_name].active_plan_id is None:
                    tradable_allowed = False
                    tradable_gate_reason = f"max_active_plans_reached={cfg.max_active_plans}"

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
                print(f"[{slot_name.upper()} DEBUG] active_count={active_count} | guard_shadow_only={cfg.shadow_only}")

                if slot_name in ("next_1", "next_2") and item:
                    use_signal = tradable_signal if tradable_signal == cfg.require_signal else "idle"
                    logs = executor.process_market_tick(
                        slot_name=slot_name,
                        event_slug=item["slug"],
                        signal=use_signal,
                        metrics=tradable_metrics,
                        secs_to_end=secs,
                        deadline_trigger=cfg.deadline_trigger_secs if slot_name == "next_1" else None,
                    )
                    for line in logs:
                        print(line)
                    if display_signal in ("watching", "armed") and tradable_signal != cfg.require_signal:
                        print(f"[DISPLAY_ONLY] {slot_name}: guard requires tradable_signal={cfg.require_signal}")

            print("\n[EXECUTOR_SNAPSHOT]")
            print(executor.snapshot())
            print("\n[BROKER_OPEN_ORDERS]")
            try:
                print([o.as_dict() for o in broker.get_open_orders()][:10])
            except Exception as exc:
                print(f"[BROKER_OPEN_ORDERS_ERROR] {type(exc).__name__}: {exc}")

        time.sleep(2.0)
