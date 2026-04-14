import time

from market.broker_startup_guard_v1 import evaluate_startup_guard
from market.broker_status_sync_v4 import sync_executor_from_broker_open_orders_v4
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


def run_live_real_smoke_cycle_v1(duration_seconds: int | None = None) -> None:
    cfg = load_live_guarded_config()
    print("[LIVE_GUARDED_CONFIG]", cfg.as_dict())

    if not cfg.enabled:
        print("[GUARD] Disabled. Set POLY_GUARDED_ENABLED=true to arm this runner.")
        return
    if cfg.shadow_only:
        print("[GUARD] live_real_smoke_cycle_v1 requires POLY_GUARDED_SHADOW_ONLY=false")
        return
    if not cfg.real_posts_enabled:
        print("[GUARD] live_real_smoke_cycle_v1 requires POLY_GUARDED_REAL_POSTS_ENABLED=true")
        return
    if cfg.allow_next_2:
        print("[GUARD] live_real_smoke_cycle_v1 requires POLY_GUARDED_ALLOW_NEXT_2=false")
        return
    if cfg.max_active_plans != 1:
        print("[GUARD] live_real_smoke_cycle_v1 requires POLY_GUARDED_MAX_ACTIVE_PLANS=1")
        return
    if cfg.min_shares_per_leg != 5:
        print("[GUARD] live_real_smoke_cycle_v1 expects POLY_GUARDED_MIN_SHARES=5")
        return

    broker = PolymarketBrokerV3.from_env()
    print(f"[BROKER_IMPL] {broker.__class__.__name__}")
    executor = Setup1BrokerExecutorV4(
        broker=broker,
        shadow_only=False,
        min_shares_per_leg=cfg.min_shares_per_leg,
    )

    health = broker.healthcheck()
    print("[BROKER_HEALTH]", health.as_dict())
    if not health.ok:
        print("[GUARD] Broker healthcheck failed; aborting smoke cycle.")
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

    run_for = duration_seconds or max(int(cfg.run_seconds), 60)
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
    tradable_metrics_next1 = None
    outcome_token_ids_next1 = None
    event_slug_next1 = None
    next_1_secs = None
    posted = False

    while time.time() - started_at < run_for:
        slot_state = _fetch_slot_state(slot_bundle)
        next_1_item = slot_bundle["queue"].get("next_1")
        if not next_1_item:
            print("[SMOKE] next_1 slot missing")
            time.sleep(2.0)
            continue

        next_1_secs = _current_secs_to_end(next_1_item.get("seconds_to_end"), started_at)
        event_slug_next1 = next_1_item["slug"]
        snap = _slot_snapshot(slot_state, "next_1")
        display_metrics, display_reason = _compute_display_metrics(snap)
        executable_metrics, executable_reason = _compute_executable_metrics(snap)

        if display_metrics:
            display_stable += 1
        else:
            display_stable = 0
        display_signal = classify_signal(display_metrics, display_stable, MIN_STABLE_SNAPSHOTS)

        active_count = sum(1 for s in executor.slots.values() if s.active_plan_id)
        tradable_allowed = active_count == 0 or executor.slots["next_1"].active_plan_id is not None
        tradable_metrics = None
        if executable_metrics and executable_metrics["sum_asks"] <= ARBITRAGE_SUM_ASKS_MAX and tradable_allowed:
            tradable_metrics = executable_metrics

        if tradable_metrics:
            tradable_stable += 1
        else:
            tradable_stable = 0
        tradable_signal = classify_signal(tradable_metrics, tradable_stable, MIN_STABLE_SNAPSHOTS)
        outcome_token_ids_next1 = _outcome_token_ids_from_snapshot(snap)
        tradable_metrics_next1 = tradable_metrics

        print("\n===== LIVE REAL SMOKE CYCLE V1 =====")
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
            logs = executor.evaluate_slot(
                slot_name="next_1",
                event_slug=event_slug_next1,
                signal=tradable_signal,
                metrics=tradable_metrics_next1,
                secs_to_end=next_1_secs,
                outcome_token_ids=outcome_token_ids_next1,
            )
            for line in logs:
                print(line)
            posted = True
            break

        time.sleep(2.0)

    if not posted:
        print(f"[SMOKE] no {cfg.require_signal} signal reached within {run_for}s")
        return

    print("\n[EXECUTOR_SNAPSHOT_AFTER_POST]")
    print(executor.snapshot())

    try:
        broker_open_orders = broker.get_open_orders()[:25]
        print("\n[BROKER_OPEN_ORDERS_AFTER_POST]")
        print([o.as_dict() for o in broker_open_orders])
        sync_logs, reconcile = sync_executor_from_broker_open_orders_v4(executor, broker_open_orders)
        for line in sync_logs:
            print(line)
        print("\n[BROKER_RECONCILE_AFTER_POST]")
        print(reconcile)
        print("\n[EXECUTOR_SNAPSHOT_AFTER_FIRST_SYNC]")
        print(executor.snapshot())
    except Exception as exc:
        print(f"[BROKER_SYNC_AFTER_POST_ERROR] {type(exc).__name__}: {exc}")
        return

    tracked_count = int(reconcile.get("tracked_count") or 0)
    external_count = int(reconcile.get("external_count") or 0)
    if tracked_count <= 0 or external_count != 0:
        print("[SMOKE] aborting cancel phase because reconcile did not confirm a clean tracked-only state")
        return

    print("\n[SMOKE] starting cancel phase via executor.on_deadline(next_1)")
    cancel_logs = executor.on_deadline(slot_name="next_1", metrics=tradable_metrics_next1)
    for line in cancel_logs:
        print(line)

    time.sleep(2.0)

    try:
        broker_open_orders_after_cancel = broker.get_open_orders()[:25]
        print("\n[BROKER_OPEN_ORDERS_AFTER_CANCEL]")
        print([o.as_dict() for o in broker_open_orders_after_cancel])
        sync_logs2, reconcile2 = sync_executor_from_broker_open_orders_v4(executor, broker_open_orders_after_cancel)
        for line in sync_logs2:
            print(line)
        print("\n[BROKER_RECONCILE_AFTER_CANCEL]")
        print(reconcile2)
        print("\n[EXECUTOR_SNAPSHOT_FINAL]")
        print(executor.snapshot())
        if broker_open_orders_after_cancel:
            print("[SMOKE_RESULT] broker still reports open orders after cancel phase")
        else:
            print("[SMOKE_RESULT] success: no open orders remain after cancel phase")
    except Exception as exc:
        print(f"[BROKER_SYNC_AFTER_CANCEL_ERROR] {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    run_live_real_smoke_cycle_v1()
