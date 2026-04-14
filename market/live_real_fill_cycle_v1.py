import os
import time

from market.broker_startup_guard_v1 import evaluate_startup_guard
from market.broker_status_sync_v4 import sync_executor_from_broker_open_orders_v4
from market.executor_state_store_v1 import (
    clear_executor_state_v1,
    flush_executor_state_v1,
    load_executor_state_v1,
    reset_executor_state_v1,
)
from market.live_guarded_config import load_live_guarded_config
from market.polymarket_broker_v3 import PolymarketBrokerV3
from market.real_execution_workflow_v2 import (
    cleanup_terminal_plan_v2,
    handle_deadline_real_v2,
    maybe_post_balanced_exit_orders_v2,
    maybe_post_force_close_exits_v2,
    maybe_take_single_leg_profit_real_v2,
)
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
from market.setup1_broker_executor_v4 import Setup1BrokerExecutorV4
from market.setup1_policy import ARBITRAGE_SUM_ASKS_MAX, classify_signal


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _outcome_token_ids_from_snapshot(snap):
    mapping = {}
    up = snap.get("up")
    down = snap.get("down")
    if up and up.get("token_id"):
        mapping["UP"] = str(up.get("token_id"))
    if down and down.get("token_id"):
        mapping["DOWN"] = str(down.get("token_id"))
    return mapping


def _validate_cfg(cfg) -> bool:
    if not cfg.enabled:
        print("[GUARD] Disabled. Set POLY_GUARDED_ENABLED=true to arm this runner.")
        return False
    if cfg.shadow_only:
        print("[GUARD] live_real_fill_cycle_v1 requires POLY_GUARDED_SHADOW_ONLY=false")
        return False
    if not cfg.real_posts_enabled:
        print("[GUARD] live_real_fill_cycle_v1 requires POLY_GUARDED_REAL_POSTS_ENABLED=true")
        return False
    if cfg.allow_next_2:
        print("[GUARD] live_real_fill_cycle_v1 requires POLY_GUARDED_ALLOW_NEXT_2=false")
        return False
    if cfg.max_active_plans != 1:
        print("[GUARD] live_real_fill_cycle_v1 requires POLY_GUARDED_MAX_ACTIVE_PLANS=1")
        return False
    if cfg.min_shares_per_leg != 5:
        print("[GUARD] live_real_fill_cycle_v1 expects POLY_GUARDED_MIN_SHARES=5")
        return False
    return True


def monitor_live_real_fill_cycle_v1(duration_seconds: int | None = None) -> None:
    cfg = load_live_guarded_config()
    print("[LIVE_GUARDED_CONFIG]", cfg.as_dict())
    if not _validate_cfg(cfg):
        return

    min_post_buffer_secs = _env_int("POLY_FILL_TEST_MIN_POST_BUFFER_SECS", 120)
    loop_sleep_secs = max(1, _env_int("POLY_FILL_TEST_LOOP_SLEEP_SECS", 2))
    run_for = int(duration_seconds or max(cfg.run_seconds, _env_int("POLY_FILL_TEST_RUN_SECONDS", 900)))
    print(
        "[FILL_CYCLE_CONFIG]",
        {
            "run_for": run_for,
            "min_post_buffer_secs": min_post_buffer_secs,
            "loop_sleep_secs": loop_sleep_secs,
            "deadline_trigger_secs": cfg.deadline_trigger_secs,
            "require_signal": cfg.require_signal,
        },
    )

    broker = PolymarketBrokerV3.from_env()
    print(f"[BROKER_IMPL] {broker.__class__.__name__}")
    executor = Setup1BrokerExecutorV4(
        broker=broker,
        shadow_only=False,
        min_shares_per_leg=cfg.min_shares_per_leg,
    )

    restore_report = load_executor_state_v1(executor)
    print("[PERSIST_RESTORE]", restore_report)

    health = broker.healthcheck()
    print("[BROKER_HEALTH]", health.as_dict())
    if not health.ok:
        print("[GUARD] Broker healthcheck failed; aborting fill cycle runner.")
        return

    try:
        startup_orders = broker.get_open_orders()[:50]
        print("[BROKER_OPEN_ORDERS_STARTUP]")
        print([o.as_dict() for o in startup_orders])
        allowed, startup_report = evaluate_startup_guard(executor, startup_orders)
        print("[STARTUP_GUARD]")
        print(startup_report)

        restored_plan_ids = restore_report.get("restored_plan_ids") or []
        if restored_plan_ids and not startup_orders and startup_report.get("tracked_count", 0) == 0:
            print("[PERSIST_STALE] restored state has no matching broker orders; clearing local persistence")
            print("[PERSIST_RESET_EXECUTOR]", reset_executor_state_v1(executor))
            print("[PERSIST_CLEAR]", clear_executor_state_v1())
            allowed, startup_report = evaluate_startup_guard(executor, startup_orders)
            print("[STARTUP_GUARD_AFTER_CLEAR]")
            print(startup_report)

        if not allowed:
            print("[GUARD] Startup blocked because external or unknown open orders exist in the broker account.")
            return

        print("[PERSIST_FLUSH_STARTUP]", flush_executor_state_v1(executor))
    except Exception as exc:
        print(f"[STARTUP_GUARD_ERROR] {type(exc).__name__}: {exc}")
        return

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
    had_active_plan = any(runtime.active_plan_id for runtime in executor.slots.values())

    while time.time() - started_at < run_for:
        slot_state = _fetch_slot_state(slot_bundle)
        next_1_item = slot_bundle["queue"].get("next_1")
        next_1_secs = _current_secs_to_end(next_1_item.get("seconds_to_end") if next_1_item else None, started_at)
        active_count = sum(1 for s in executor.slots.values() if s.active_plan_id)

        tradable_metrics_next1 = None
        tradable_signal_next1 = "idle"
        outcome_token_ids_next1 = None
        event_slug_next1 = next_1_item["slug"] if next_1_item else None

        if time.time() >= next_print:
            next_print = time.time() + 2.0
            print("\n===== LIVE REAL FILL CYCLE V1 SNAPSHOT =====")

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

            tradable_allowed = slot_name == "next_1" and (active_count == 0 or executor.slots["next_1"].active_plan_id is not None)
            tradable_gate_reason = "next_1_only" if slot_name == "next_1" else "next_2_disabled_first_live"
            tradable_metrics = None
            if executable_metrics and executable_metrics["sum_asks"] <= ARBITRAGE_SUM_ASKS_MAX and tradable_allowed:
                tradable_metrics = executable_metrics

            if tradable_metrics:
                tradable_stable_counts[slot_name] += 1
            else:
                tradable_stable_counts[slot_name] = 0
            tradable_signal = classify_signal(tradable_metrics, tradable_stable_counts[slot_name], MIN_STABLE_SNAPSHOTS)

            if time.time() < next_print - 1.5:
                pass
            else:
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
                print(f"[{slot_name.upper()} DEBUG] active_count={active_count} | shadow_only={executor.shadow_only}")

            if slot_name == "next_1":
                tradable_metrics_next1 = tradable_metrics
                tradable_signal_next1 = tradable_signal
                outcome_token_ids_next1 = _outcome_token_ids_from_snapshot(snap)

        runtime = executor.slots["next_1"]
        plan = executor.order_manager.get_plan(runtime.active_plan_id) if runtime.active_plan_id else None

        if plan is None and event_slug_next1 and tradable_signal_next1 == cfg.require_signal:
            if next_1_secs is not None and next_1_secs <= int(cfg.deadline_trigger_secs) + min_post_buffer_secs:
                print(
                    f"[ENTRY_GATE] next_1 signal armed but secs_to_end={next_1_secs} is too close to deadline trigger={cfg.deadline_trigger_secs}; waiting for safer window"
                )
            else:
                logs = executor.evaluate_slot(
                    slot_name="next_1",
                    event_slug=event_slug_next1,
                    signal=tradable_signal_next1,
                    metrics=tradable_metrics_next1,
                    secs_to_end=next_1_secs,
                    outcome_token_ids=outcome_token_ids_next1,
                )
                for line in logs:
                    print(line)
                if executor.slots["next_1"].active_plan_id:
                    had_active_plan = True
                print("[PERSIST_FLUSH_AFTER_EVALUATE]", flush_executor_state_v1(executor))
                runtime = executor.slots["next_1"]
                plan = executor.order_manager.get_plan(runtime.active_plan_id) if runtime.active_plan_id else None

        try:
            broker_open_orders = broker.get_open_orders()[:50]
            print("\n[BROKER_OPEN_ORDERS]")
            print([o.as_dict() for o in broker_open_orders])
            sync_logs, reconcile = sync_executor_from_broker_open_orders_v4(executor, broker_open_orders)
            for line in sync_logs:
                print(line)

            tp_logs = maybe_take_single_leg_profit_real_v2(executor, slot_name="next_1", metrics=tradable_metrics_next1)
            for line in tp_logs:
                print(line)

            balanced_logs = maybe_post_balanced_exit_orders_v2(executor, slot_name="next_1", metrics=tradable_metrics_next1)
            for line in balanced_logs:
                print(line)

            deadline_logs = handle_deadline_real_v2(
                executor,
                slot_name="next_1",
                secs_to_end=next_1_secs,
                deadline_trigger=cfg.deadline_trigger_secs,
                metrics=tradable_metrics_next1,
            )
            for line in deadline_logs:
                print(line)

            fc_logs = maybe_post_force_close_exits_v2(executor, slot_name="next_1", metrics=tradable_metrics_next1)
            for line in fc_logs:
                print(line)

            cleanup_logs = cleanup_terminal_plan_v2(executor, slot_name="next_1")
            for line in cleanup_logs:
                print(line)

            print("\n[BROKER_RECONCILE]")
            print(reconcile)
            print("\n[EXECUTOR_SNAPSHOT_AFTER_SYNC]")
            print(executor.snapshot())
            print("[PERSIST_FLUSH_AFTER_SYNC]", flush_executor_state_v1(executor))
        except Exception as exc:
            print(f"[BROKER_LOOP_ERROR] {type(exc).__name__}: {exc}")

        runtime = executor.slots["next_1"]
        broker_open_orders_after = broker.get_open_orders()[:50]
        if had_active_plan and runtime.active_plan_id is None and not broker_open_orders_after:
            print("[FILL_CYCLE_RESULT] terminal cleanup reached with no active plan and no broker open orders")
            print("[PERSIST_FINAL]", flush_executor_state_v1(executor))
            return

        time.sleep(loop_sleep_secs)

    print("[FILL_CYCLE_RESULT] run finished before terminal cleanup")
    print("[EXECUTOR_SNAPSHOT_FINAL]")
    print(executor.snapshot())
    print("[PERSIST_FINAL]", flush_executor_state_v1(executor))


if __name__ == "__main__":
    monitor_live_real_fill_cycle_v1()
