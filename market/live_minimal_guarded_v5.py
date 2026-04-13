import time

from market.broker_startup_guard_v1 import evaluate_startup_guard
from market.broker_status_sync_v3 import sync_executor_from_broker_open_orders_v3
from market.hybrid_focus_policy_v1 import evaluate_hybrid_slot_focus_v1
from market.hybrid_single_leg_profit_v2 import maybe_take_single_leg_profit_v2
from market.live_guarded_config import load_live_guarded_config
from market.polymarket_broker_v2 import PolymarketBrokerV2
from market.rest_5m_shadow_public_v5 import (
    MIN_STABLE_SNAPSHOTS,
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


def monitor_live_minimal_guarded_v5(duration_seconds: int | None = None) -> None:
    cfg = load_live_guarded_config()
    print("[LIVE_GUARDED_CONFIG]", cfg.as_dict())

    if not cfg.enabled:
        print("[GUARD] Disabled. Set POLY_GUARDED_ENABLED=true to arm this runner.")
        return

    if not cfg.shadow_only and not cfg.real_posts_enabled:
        print("[GUARD] Refusing to run because real posts are disabled by config.")
        return

    if not cfg.shadow_only and cfg.real_posts_enabled:
        print("[GUARD] Real posting is intentionally blocked in v5 until live fill reconciliation + real flatten are implemented.")
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

    try:
        startup_orders = broker.get_open_orders()[:50]
        print("[BROKER_OPEN_ORDERS_STARTUP]")
        print([o.as_dict() for o in startup_orders])
        allowed, startup_report = evaluate_startup_guard(executor, startup_orders)
        print("[STARTUP_GUARD]")
        print(startup_report)
        if not allowed:
            print("[GUARD] Startup blocked because external or unknown open orders exist in the broker account.")
            return
    except Exception as exc:
        print(f"[STARTUP_GUARD_ERROR] {type(exc).__name__}: {exc}")
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
            print("\n===== LIVE MINIMAL GUARDED V5 SNAPSHOT =====")
            next_1_item = slot_bundle["queue"].get("next_1")
            next_1_secs = _current_secs_to_end(next_1_item.get("seconds_to_end") if next_1_item else None, started_at)
            active_count = sum(1 for s in executor.slots.values() if s.active_plan_id)
            next_1_active_plan_id = executor.slots["next_1"].active_plan_id

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

                tradable_allowed, tradable_gate_reason = evaluate_hybrid_slot_focus_v1(
                    slot_name=slot_name,
                    next_1_secs_to_end=next_1_secs,
                    next_1_active_plan_id=next_1_active_plan_id,
                    max_active_plans_reached=(active_count >= cfg.max_active_plans and (executor.slots.get(slot_name) and executor.slots[slot_name].active_plan_id is None)),
                    allow_next_2_config=cfg.allow_next_2,
                ) if slot_name in ("next_1", "next_2") else (False, "non_tradable_slot")

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
                    tp_logs = maybe_take_single_leg_profit_v2(
                        executor,
                        slot_name=slot_name,
                        metrics=tradable_metrics,
                    )
                    for line in tp_logs:
                        print(line)
                    if display_signal in ("watching", "armed") and tradable_signal != cfg.require_signal:
                        print(f"[DISPLAY_ONLY] {slot_name}: guard requires tradable_signal={cfg.require_signal}")

            print("\n[EXECUTOR_SNAPSHOT_BEFORE_SYNC]")
            print(executor.snapshot())
            print("\n[BROKER_OPEN_ORDERS]")
            try:
                broker_open_orders = broker.get_open_orders()[:25]
                print([o.as_dict() for o in broker_open_orders])
                sync_logs, reconcile = sync_executor_from_broker_open_orders_v3(executor, broker_open_orders)
                for line in sync_logs:
                    print(line)
                print("\n[BROKER_RECONCILE]")
                print(reconcile)
                print("\n[EXECUTOR_SNAPSHOT_AFTER_SYNC]")
                print(executor.snapshot())
            except Exception as exc:
                print(f"[BROKER_OPEN_ORDERS_ERROR] {type(exc).__name__}: {exc}")

        time.sleep(2.0)
