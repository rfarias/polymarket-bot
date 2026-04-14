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
from market.rest_5m_shadow_public_v5 import (
    MIN_STABLE_SNAPSHOTS,
    _build_slot_bundle,
    _compute_display_metrics,
    _compute_executable_metrics,
    _current_secs_to_end,
    _fetch_slot_state,
    _slot_snapshot,
)
from market.setup1_broker_executor_v4 import Setup1BrokerExecutorV4
from market.setup1_policy import ARBITRAGE_SUM_ASKS_MAX, classify_signal


def _outcome_token_ids_from_snapshot(snap):
    mapping = {}
    up = snap.get("up")
    down = snap.get("down")
    if up and up.get("token_id"):
        mapping["UP"] = str(up.get("token_id"))
    if down and down.get("token_id"):
        mapping["DOWN"] = str(down.get("token_id"))
    return mapping


def _build_executor(cfg):
    broker = PolymarketBrokerV3.from_env()
    print(f"[BROKER_IMPL] {broker.__class__.__name__}")
    executor = Setup1BrokerExecutorV4(
        broker=broker,
        shadow_only=False,
        min_shares_per_leg=cfg.min_shares_per_leg,
    )
    return broker, executor


def _validate_cfg(cfg) -> bool:
    if not cfg.enabled:
        print("[GUARD] Disabled. Set POLY_GUARDED_ENABLED=true to arm this runner.")
        return False
    if cfg.shadow_only:
        print("[GUARD] live_real_persist_restart_probe_v1 requires POLY_GUARDED_SHADOW_ONLY=false")
        return False
    if not cfg.real_posts_enabled:
        print("[GUARD] live_real_persist_restart_probe_v1 requires POLY_GUARDED_REAL_POSTS_ENABLED=true")
        return False
    if cfg.allow_next_2:
        print("[GUARD] live_real_persist_restart_probe_v1 requires POLY_GUARDED_ALLOW_NEXT_2=false")
        return False
    if cfg.max_active_plans != 1:
        print("[GUARD] live_real_persist_restart_probe_v1 requires POLY_GUARDED_MAX_ACTIVE_PLANS=1")
        return False
    if cfg.min_shares_per_leg != 5:
        print("[GUARD] live_real_persist_restart_probe_v1 expects POLY_GUARDED_MIN_SHARES=5")
        return False
    return True


def _phase_two_restore_and_cleanup(cfg, broker, executor, restore_report):
    print("[PERSIST_PROBE_PHASE] restore_and_cleanup")
    health = broker.healthcheck()
    print("[BROKER_HEALTH]", health.as_dict())
    if not health.ok:
        print("[GUARD] Broker healthcheck failed during restore phase.")
        return

    broker_open_orders = broker.get_open_orders()[:50]
    print("[BROKER_OPEN_ORDERS_STARTUP]")
    print([o.as_dict() for o in broker_open_orders])
    allowed, startup_report = evaluate_startup_guard(executor, broker_open_orders)
    print("[STARTUP_GUARD]")
    print(startup_report)

    if not allowed:
        print("[PERSIST_PROBE_FAIL] restore startup guard blocked")
        return

    tracked_count = int(startup_report.get("tracked_count") or 0)
    restored_plan_ids = restore_report.get("restored_plan_ids") or []
    if not restored_plan_ids:
        print("[PERSIST_PROBE_FAIL] no restored plan ids found in phase two")
        return
    if tracked_count <= 0:
        print("[PERSIST_PROBE_FAIL] restored state did not match any broker open orders")
        print("[PERSIST_RESET_EXECUTOR]", reset_executor_state_v1(executor))
        print("[PERSIST_CLEAR]", clear_executor_state_v1())
        return

    print("[PERSIST_PROBE_OK] restored state matched live broker orders")
    sync_logs, reconcile = sync_executor_from_broker_open_orders_v4(executor, broker_open_orders)
    for line in sync_logs:
        print(line)
    print("[BROKER_RECONCILE]")
    print(reconcile)
    print("[EXECUTOR_SNAPSHOT_AFTER_RESTORE]")
    print(executor.snapshot())

    restored_slot_name = None
    for runtime in executor.slots.values():
        if runtime.active_plan_id in restored_plan_ids:
            restored_slot_name = runtime.slot_name
            break
    if restored_slot_name is None:
        restored_slot_name = "next_1"

    print(f"[PERSIST_PROBE] cleanup restored orders via executor.on_deadline({restored_slot_name})")
    cancel_logs = executor.on_deadline(slot_name=restored_slot_name, metrics=None)
    for line in cancel_logs:
        print(line)
    time.sleep(2.0)

    broker_open_orders_after = broker.get_open_orders()[:50]
    print("[BROKER_OPEN_ORDERS_AFTER_CANCEL]")
    print([o.as_dict() for o in broker_open_orders_after])
    sync_logs2, reconcile2 = sync_executor_from_broker_open_orders_v4(executor, broker_open_orders_after)
    for line in sync_logs2:
        print(line)
    print("[BROKER_RECONCILE_AFTER_CANCEL]")
    print(reconcile2)
    print("[EXECUTOR_SNAPSHOT_FINAL]")
    print(executor.snapshot())
    print("[PERSIST_FLUSH_FINAL]", flush_executor_state_v1(executor))
    if broker_open_orders_after:
        print("[PERSIST_PROBE_RESULT] cleanup_incomplete")
    else:
        print("[PERSIST_PROBE_RESULT] success")


def _phase_one_post_and_exit(cfg, broker, executor):
    print("[PERSIST_PROBE_PHASE] post_and_exit")
    health = broker.healthcheck()
    print("[BROKER_HEALTH]", health.as_dict())
    if not health.ok:
        print("[GUARD] Broker healthcheck failed during phase one.")
        return

    startup_orders = broker.get_open_orders()[:50]
    print("[BROKER_OPEN_ORDERS_STARTUP]")
    print([o.as_dict() for o in startup_orders])
    allowed, startup_report = evaluate_startup_guard(executor, startup_orders)
    print("[STARTUP_GUARD]")
    print(startup_report)
    if not allowed:
        print("[GUARD] Startup blocked because external or unknown open orders exist in the broker account.")
        return

    slot_bundle = _build_slot_bundle()
    print("[QUEUE] 5m queue summary:")
    for slot_name in ("current", "next_1", "next_2"):
        item = slot_bundle["queue"].get(slot_name)
        if item:
            print(f"- {slot_name}: {item['seconds_to_end']}s | {item['title']} | slug={item['slug']}")
        else:
            print(f"- {slot_name}: none")

    display_stable = 0
    tradable_stable = 0
    started_at = time.time()
    run_for = max(int(cfg.run_seconds), 60)
    while time.time() - started_at < run_for:
        slot_state = _fetch_slot_state(slot_bundle)
        next_1_item = slot_bundle["queue"].get("next_1")
        if not next_1_item:
            print("[PERSIST_PROBE] next_1 missing")
            time.sleep(2.0)
            continue

        next_1_secs = _current_secs_to_end(next_1_item.get("seconds_to_end"), started_at)
        snap = _slot_snapshot(slot_state, "next_1")
        display_metrics, display_reason = _compute_display_metrics(snap)
        executable_metrics, executable_reason = _compute_executable_metrics(snap)

        if display_metrics:
            display_stable += 1
        else:
            display_stable = 0
        display_signal = classify_signal(display_metrics, display_stable, MIN_STABLE_SNAPSHOTS)

        tradable_metrics = None
        if executable_metrics and executable_metrics["sum_asks"] <= ARBITRAGE_SUM_ASKS_MAX:
            tradable_metrics = executable_metrics
        if tradable_metrics:
            tradable_stable += 1
        else:
            tradable_stable = 0
        tradable_signal = classify_signal(tradable_metrics, tradable_stable, MIN_STABLE_SNAPSHOTS)

        print("\n===== LIVE REAL PERSIST RESTART PROBE V1 =====")
        print(
            f"[NEXT_1] secs_to_end={next_1_secs} | display_stable={display_stable} | tradable_stable={tradable_stable} | "
            f"display_signal={display_signal} | tradable_signal={tradable_signal}"
        )
        if display_metrics:
            print(
                f"DISPLAY asks={display_metrics['up_ask']}/{display_metrics['down_ask']} | "
                f"bids={display_metrics['up_bid']}/{display_metrics['down_bid']} | "
                f"sum_asks={display_metrics['sum_asks']} | sum_bids={display_metrics['sum_bids']}"
            )
        else:
            print(f"display_metrics=None | reason={display_reason}")
        if executable_metrics:
            print(
                f"EXECUTABLE asks={executable_metrics['up_ask']}/{executable_metrics['down_ask']} | "
                f"bids={executable_metrics['up_bid']}/{executable_metrics['down_bid']} | "
                f"sum_asks={executable_metrics['sum_asks']} | sum_bids={executable_metrics['sum_bids']}"
            )
        else:
            print(f"executable_metrics=None | reason={executable_reason}")

        if tradable_signal == cfg.require_signal:
            outcome_token_ids = _outcome_token_ids_from_snapshot(snap)
            logs = executor.evaluate_slot(
                slot_name="next_1",
                event_slug=next_1_item["slug"],
                signal=tradable_signal,
                metrics=tradable_metrics,
                secs_to_end=next_1_secs,
                outcome_token_ids=outcome_token_ids,
            )
            for line in logs:
                print(line)
            print("[PERSIST_FLUSH_AFTER_POST]", flush_executor_state_v1(executor))
            broker_open_orders = broker.get_open_orders()[:50]
            print("[BROKER_OPEN_ORDERS_AFTER_POST]")
            print([o.as_dict() for o in broker_open_orders])
            sync_logs, reconcile = sync_executor_from_broker_open_orders_v4(executor, broker_open_orders)
            for line in sync_logs:
                print(line)
            print("[BROKER_RECONCILE_AFTER_POST]")
            print(reconcile)
            print("[EXECUTOR_SNAPSHOT_AFTER_POST]")
            print(executor.snapshot())
            print("[PERSIST_PROBE_READY] state saved and live orders remain open; rerun same command to validate restore")
            return

        time.sleep(2.0)

    print(f"[PERSIST_PROBE] no {cfg.require_signal} signal reached within {run_for}s")


def run_live_real_persist_restart_probe_v1() -> None:
    cfg = load_live_guarded_config()
    print("[LIVE_GUARDED_CONFIG]", cfg.as_dict())
    if not _validate_cfg(cfg):
        return

    broker, executor = _build_executor(cfg)
    restore_report = load_executor_state_v1(executor)
    print("[PERSIST_RESTORE]", restore_report)
    restored_plan_ids = restore_report.get("restored_plan_ids") or []

    if restored_plan_ids:
        _phase_two_restore_and_cleanup(cfg, broker, executor, restore_report)
    else:
        _phase_one_post_and_exit(cfg, broker, executor)


if __name__ == "__main__":
    run_live_real_persist_restart_probe_v1()
