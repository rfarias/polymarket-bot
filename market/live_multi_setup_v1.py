from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, Optional

from market.broker_startup_guard_v1 import evaluate_startup_guard
from market.broker_status_sync_v4 import sync_executor_from_broker_open_orders_v4
from market.broker_types import BrokerOrderRequest
from market.continuation_filter_v1 import ContinuationRiskFilterV1
from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.executor_state_store_v1 import clear_executor_state_v1, flush_executor_state_v1, load_executor_state_v1, reset_executor_state_v1
from market.live_guarded_config import load_live_guarded_config
from market.polymarket_broker_v3 import PolymarketBrokerV3
from market.real_execution_workflow_v2 import (
    cleanup_terminal_plan_v2,
    handle_deadline_real_v2,
    maybe_post_balanced_exit_orders_v2,
    maybe_post_force_close_exits_v2,
    maybe_take_single_leg_profit_real_v2,
)
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _current_secs_to_end, _fetch_slot_state, _slot_snapshot
from market.setup1_broker_executor_v4 import Setup1BrokerExecutorV4
from market.setup1_policy import ARBITRAGE_SUM_ASKS_MAX, classify_signal
from market.slug_discovery import fetch_event_by_slug


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _tick_size_from_snap(snap: Dict, outcome: str) -> float:
    side = (snap.get("up") if outcome == "UP" else snap.get("down")) or {}
    tick = _safe_float(side.get("tick_size"), 0.01)
    return max(0.001, tick)


@dataclass
class CurrentTradeStateV1:
    strategy: Optional[str] = None  # scalp | almost_resolved
    shadow_only: bool = True
    mode: str = "idle"  # idle | pending_entry | open_position | pending_exit
    event_slug: Optional[str] = None
    outcome: Optional[str] = None
    token_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    entry_price: Optional[float] = None
    filled_qty: float = 0.0
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_reason: Optional[str] = None


def _validate_cfg(cfg) -> bool:
    if not cfg.enabled:
        print("[GUARD] Disabled. Set POLY_GUARDED_ENABLED=true to arm this runner.")
        return False
    if cfg.shadow_only:
        print("[GUARD] live_multi_setup_v1 requires POLY_GUARDED_SHADOW_ONLY=false")
        return False
    if not cfg.real_posts_enabled:
        print("[GUARD] live_multi_setup_v1 requires POLY_GUARDED_REAL_POSTS_ENABLED=true")
        return False
    if cfg.allow_next_2:
        print("[GUARD] live_multi_setup_v1 requires POLY_GUARDED_ALLOW_NEXT_2=false")
        return False
    if cfg.max_active_plans != 1:
        print("[GUARD] live_multi_setup_v1 requires POLY_GUARDED_MAX_ACTIVE_PLANS=1")
        return False
    if cfg.min_shares_per_leg != 5:
        print("[GUARD] live_multi_setup_v1 expects POLY_GUARDED_MIN_SHARES=5")
        return False
    return True


def _get_order_status(broker, order_id: str):
    if not order_id:
        return None
    try:
        order = broker.get_order(order_id)
        if order is not None:
            return order
    except Exception:
        pass
    try:
        for order in broker.get_open_orders()[:50]:
            if order.order_id == order_id:
                return order
    except Exception:
        pass
    return None


def _reset_current_trade(now: float) -> CurrentTradeStateV1:
    return CurrentTradeStateV1(mode="idle", shadow_only=True, created_at=now, updated_at=now)


def _post_current_entry(
    broker,
    state: CurrentTradeStateV1,
    *,
    event_slug: str,
    token_id: str,
    outcome: str,
    entry_price: float,
    qty: float,
    tick_size: float,
    strategy: str,
    now: float,
    target_ticks: int,
    stop_ticks: int,
    shadow_only: bool,
) -> CurrentTradeStateV1:
    state.strategy = strategy
    state.shadow_only = bool(shadow_only)
    state.mode = "pending_entry"
    state.event_slug = event_slug
    state.outcome = outcome
    state.token_id = token_id
    state.entry_order_id = None
    state.entry_price = float(entry_price)
    state.filled_qty = 0.0
    state.target_price = round(min(0.99, float(entry_price) + target_ticks * tick_size), 6)
    state.stop_price = round(max(0.01, float(entry_price) - stop_ticks * tick_size), 6)
    state.created_at = now
    state.updated_at = now
    state.last_reason = f"entry_posted:{strategy}"

    if shadow_only:
        state.entry_order_id = f"shadow:{strategy}:entry:{int(now)}:{outcome}"
        print(
            f"[CURRENT_ENTRY_SHADOW] strategy={strategy} outcome={outcome} entry={entry_price} "
            f"target={state.target_price} stop={state.stop_price}"
        )
        return state

    req = BrokerOrderRequest(
        token_id=token_id,
        side="BUY",
        price=float(entry_price),
        size=float(qty),
        market_slug=event_slug,
        outcome=outcome,
        client_order_key=f"current_{strategy}:{int(now)}:{outcome}",
    )
    order = broker.place_limit_order(req)
    state.entry_order_id = order.order_id
    print(
        f"[CURRENT_ENTRY] strategy={strategy} outcome={outcome} entry={entry_price} "
        f"target={state.target_price} stop={state.stop_price} order_id={order.order_id}"
    )
    return state


def _post_current_exit(broker, state: CurrentTradeStateV1, *, bid_now: float, now: float, reason: str) -> CurrentTradeStateV1:
    qty = round(float(state.filled_qty or 0.0), 6)
    if qty <= 0:
        return _reset_current_trade(now)

    if state.shadow_only:
        print(
            f"[CURRENT_EXIT_SHADOW] strategy={state.strategy} reason={reason} "
            f"entry={state.entry_price} exit_bid={bid_now} qty={qty}"
        )
        return _reset_current_trade(now)

    req = BrokerOrderRequest(
        token_id=state.token_id or "",
        side="SELL",
        price=float(bid_now),
        size=qty,
        market_slug=state.event_slug,
        outcome=state.outcome,
        client_order_key=f"current_exit:{reason}:{int(now)}:{state.outcome}",
    )
    order = broker.place_limit_order(req)
    state.mode = "pending_exit"
    state.exit_order_id = order.order_id
    state.updated_at = now
    state.last_reason = f"exit_posted:{reason}"
    print(f"[CURRENT_EXIT] strategy={state.strategy} reason={reason} bid={bid_now} order_id={order.order_id}")
    return state


def _manage_current_trade(
    broker,
    state: CurrentTradeStateV1,
    *,
    current_snap: Dict,
    secs_to_end: Optional[int],
    cfg_scalp: CurrentScalpConfigV1,
    cfg_resolved: CurrentAlmostResolvedConfigV1,
    now: float,
) -> CurrentTradeStateV1:
    if state.mode == "idle":
        return state

    if state.mode == "pending_entry":
        if state.shadow_only:
            state.filled_qty = max(state.filled_qty, 1.0)
            state.mode = "open_position"
            state.updated_at = now
            print(
                f"[CURRENT_ENTRY_FILLED_SHADOW] strategy={state.strategy} "
                f"outcome={state.outcome} entry={state.entry_price}"
            )
            return state
        order = _get_order_status(broker, state.entry_order_id or "")
        state.filled_qty = max(state.filled_qty, _safe_float(getattr(order, "size_matched", None), 0.0))
        if state.filled_qty > 0:
            state.mode = "open_position"
            state.updated_at = now
            if order and getattr(order, "remaining_size", 0.0) > 0:
                print("[CURRENT_ENTRY_CANCEL_REMAINDER]", broker.cancel_order(state.entry_order_id))
            return state
        timeout = cfg_resolved.max_hold_secs if state.strategy == "almost_resolved" else cfg_scalp.entry_timeout_secs
        if now - state.created_at >= timeout:
            print("[CURRENT_ENTRY_TIMEOUT_CANCEL]", broker.cancel_order(state.entry_order_id))
            return _reset_current_trade(now)
        return state

    executable, _ = _compute_executable_metrics(current_snap)
    if executable is None:
        return state
    bid_now = _safe_float(executable.get("up_bid" if state.outcome == "UP" else "down_bid"), 0.0)

    if state.mode == "open_position":
        if bid_now >= _safe_float(state.target_price, 0.0):
            return _post_current_exit(broker, state, bid_now=bid_now, now=now, reason="target")
        if bid_now <= _safe_float(state.stop_price, 0.0):
            return _post_current_exit(broker, state, bid_now=bid_now, now=now, reason="stop")
        if secs_to_end is not None and secs_to_end <= 8:
            return _post_current_exit(broker, state, bid_now=bid_now, now=now, reason="expiry_near")
        timeout = cfg_resolved.max_hold_secs if state.strategy == "almost_resolved" else cfg_scalp.position_timeout_secs
        if now - state.updated_at >= timeout:
            return _post_current_exit(broker, state, bid_now=bid_now, now=now, reason="timeout")
        return state

    if state.mode == "pending_exit":
        order = _get_order_status(broker, state.exit_order_id or "")
        if order is None:
            return _reset_current_trade(now)
        status = str(getattr(order, "status", "") or "").lower()
        if status in ("filled", "closed", "resolved"):
            return _reset_current_trade(now)
        timeout = cfg_resolved.max_hold_secs if state.strategy == "almost_resolved" else cfg_scalp.position_timeout_secs
        if now - state.updated_at >= timeout:
            print("[CURRENT_EXIT_TIMEOUT_CANCEL]", broker.cancel_order(state.exit_order_id))
            return _reset_current_trade(now)
    return state


def monitor_live_multi_setup_v1(duration_seconds: int | None = None) -> None:
    cfg = load_live_guarded_config()
    print("[LIVE_GUARDED_CONFIG]", cfg.as_dict())
    if not _validate_cfg(cfg):
        return

    next1_deadline_secs = _env_int("POLY_MULTI_NEXT1_CANCEL_SECS", 360)
    run_for = int(duration_seconds or max(cfg.run_seconds, _env_int("POLY_MULTI_RUN_SECONDS", 900)))
    current_scalp_cfg = CurrentScalpConfigV1()
    current_resolved_cfg = CurrentAlmostResolvedConfigV1()
    current_scalp_shadow_only = _env_bool("POLY_CURRENT_SCALP_SHADOW_ONLY", True)
    current_almost_resolved_shadow_only = _env_bool("POLY_CURRENT_ALMOST_RESOLVED_SHADOW_ONLY", True)
    print(
        "[MULTI_SETUP_CONFIG]",
        {
            "run_for": run_for,
            "next1_deadline_secs": next1_deadline_secs,
            "current_scalp_shadow_only": current_scalp_shadow_only,
            "current_almost_resolved_shadow_only": current_almost_resolved_shadow_only,
            "current_scalp": current_scalp_cfg.as_dict(),
            "current_almost_resolved": current_resolved_cfg.as_dict(),
        },
    )

    broker = PolymarketBrokerV3.from_env()
    executor = Setup1BrokerExecutorV4(broker=broker, shadow_only=False, min_shares_per_leg=cfg.min_shares_per_leg)
    restore_report = load_executor_state_v1(executor)
    print("[PERSIST_RESTORE]", restore_report)

    health = broker.healthcheck()
    print("[BROKER_HEALTH]", health.as_dict())
    if not health.ok:
        print("[GUARD] Broker healthcheck failed; aborting multi setup runner.")
        return

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

    slot_bundle = _build_slot_bundle()
    continuation_filter = ContinuationRiskFilterV1()
    current_research = CurrentScalpResearchV1(cfg=current_scalp_cfg)
    current_trade = _reset_current_trade(time.time())
    current_open_reference: Dict[str, Optional[float]] = {"slug": None, "price": None}
    pending_almost_resolved_window = False

    started_at = time.time()
    display_stable_next1 = 0

    while time.time() - started_at < run_for:
        now = time.time()
        slot_state = _fetch_slot_state(slot_bundle)

        current_item = slot_bundle["queue"].get("current")
        next1_item = slot_bundle["queue"].get("next_1")
        current_secs = _current_secs_to_end(current_item.get("seconds_to_end") if current_item else None, started_at)
        next1_secs = _current_secs_to_end(next1_item.get("seconds_to_end") if next1_item else None, started_at)

        current_snap = _slot_snapshot(slot_state, "current")
        next1_snap = _slot_snapshot(slot_state, "next_1")
        next1_exec, _ = _compute_executable_metrics(next1_snap)

        tradable_metrics_next1 = None
        tradable_signal_next1 = "idle"
        outcome_token_ids_next1 = None
        if next1_exec:
            cont = continuation_filter.update_and_classify(slot_name="next_1", snap=next1_snap, now_ts=now)
            if next1_exec["sum_asks"] <= ARBITRAGE_SUM_ASKS_MAX and not cont.get("block_entry"):
                tradable_metrics_next1 = next1_exec
                display_stable_next1 += 1
            else:
                display_stable_next1 = 0
            tradable_signal_next1 = classify_signal(tradable_metrics_next1, display_stable_next1, 2)
            up = next1_snap.get("up") or {}
            down = next1_snap.get("down") or {}
            outcome_token_ids_next1 = {}
            if up.get("token_id"):
                outcome_token_ids_next1["UP"] = str(up.get("token_id"))
            if down.get("token_id"):
                outcome_token_ids_next1["DOWN"] = str(down.get("token_id"))
            print(
                f"[NEXT1] secs={next1_secs} signal={tradable_signal_next1} "
                f"sum_asks={next1_exec['sum_asks']} cont_label={cont.get('label')} cont_score={cont.get('score')}"
            )

        runtime = executor.slots["next_1"]
        plan = executor.order_manager.get_plan(runtime.active_plan_id) if runtime.active_plan_id else None
        if plan is None and next1_item and tradable_signal_next1 == cfg.require_signal and next1_secs and next1_secs > next1_deadline_secs:
            logs = executor.evaluate_slot(
                slot_name="next_1",
                event_slug=next1_item["slug"],
                signal=tradable_signal_next1,
                metrics=tradable_metrics_next1,
                secs_to_end=next1_secs,
                outcome_token_ids=outcome_token_ids_next1,
            )
            for line in logs:
                print(line)
            print("[PERSIST_FLUSH_AFTER_EVALUATE]", flush_executor_state_v1(executor))

        broker_open_orders = broker.get_open_orders()[:50]
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
            secs_to_end=next1_secs,
            deadline_trigger=next1_deadline_secs,
            metrics=tradable_metrics_next1,
        )
        for line in deadline_logs:
            print(line)
        if any("deadline with no fills -> aborted cleanly" in line for line in deadline_logs):
            pending_almost_resolved_window = True
        fc_logs = maybe_post_force_close_exits_v2(executor, slot_name="next_1", metrics=tradable_metrics_next1)
        for line in fc_logs:
            print(line)
        cleanup_logs = cleanup_terminal_plan_v2(executor, slot_name="next_1")
        for line in cleanup_logs:
            print(line)
        print("[BROKER_RECONCILE]", reconcile)
        print("[PERSIST_FLUSH_AFTER_SYNC]", flush_executor_state_v1(executor))

        if current_item and current_item["slug"] != current_open_reference.get("slug"):
            raw_event = fetch_event_by_slug(current_item["slug"])
            market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
            start_time = market.get("eventStartTime") or raw_event.get("startTime") if raw_event else None
            opened = fetch_binance_open_price_for_event_start_v1(start_time) if start_time else {"open_price": None}
            current_open_reference = {"slug": current_item["slug"], "price": opened.get("open_price"), "event_start_time": start_time}
            print("[CURRENT_OPEN_REFERENCE]", current_open_reference)

        ref = fetch_external_btc_reference_v1() if current_item else {}
        current_signal = current_research.evaluate(
            snap=current_snap,
            secs_to_end=current_secs,
            event_start_time=current_open_reference.get("event_start_time"),
            now_ts=now,
            reference_price=ref.get("reference_price"),
            source_divergence_bps=ref.get("source_divergence_bps"),
            opening_reference_price=current_open_reference.get("price"),
        ) if current_item else {"setup": "no_edge", "allow": False}

        almost_resolved_signal = evaluate_current_almost_resolved_v1(
            snap=current_snap,
            secs_to_end=current_secs,
            reference_signal=current_signal,
            cfg=current_resolved_cfg,
        ) if current_item else {"allow": False}

        print(
            f"[CURRENT] secs={current_secs} scalp_setup={current_signal.get('setup')} "
            f"scalp_allow={current_signal.get('allow')} almost_allow={almost_resolved_signal.get('allow')} "
            f"pending_almost_resolved_window={pending_almost_resolved_window} current_trade_mode={current_trade.mode}"
        )

        if current_trade.mode == "idle" and current_item:
            if runtime.active_plan_id and current_signal.get("allow") and current_signal.get("setup") in ("continuation", "reversal"):
                side = current_signal.get("side")
                token_id = str((current_snap.get("up") if side == "UP" else current_snap.get("down") or {}).get("token_id") or "")
                tick = _tick_size_from_snap(current_snap, side)
                current_trade = _post_current_entry(
                    broker,
                    current_trade,
                    event_slug=current_item["slug"],
                    token_id=token_id,
                    outcome=side,
                    entry_price=float(current_signal.get("entry_price")),
                    qty=1.0,
                    tick_size=tick,
                    strategy="scalp",
                    now=now,
                    target_ticks=current_scalp_cfg.target_ticks,
                    stop_ticks=current_scalp_cfg.stop_ticks,
                    shadow_only=current_scalp_shadow_only,
                )
            elif pending_almost_resolved_window and almost_resolved_signal.get("allow"):
                side = almost_resolved_signal.get("side")
                token_id = str((current_snap.get("up") if side == "UP" else current_snap.get("down") or {}).get("token_id") or "")
                tick = _tick_size_from_snap(current_snap, side)
                current_trade = _post_current_entry(
                    broker,
                    current_trade,
                    event_slug=current_item["slug"],
                    token_id=token_id,
                    outcome=side,
                    entry_price=float(almost_resolved_signal.get("entry_price")),
                    qty=1.0,
                    tick_size=tick,
                    strategy="almost_resolved",
                    now=now,
                    target_ticks=current_resolved_cfg.target_ticks,
                    stop_ticks=current_resolved_cfg.stop_ticks,
                    shadow_only=current_almost_resolved_shadow_only,
                )
                pending_almost_resolved_window = False

        current_trade = _manage_current_trade(
            broker,
            current_trade,
            current_snap=current_snap,
            secs_to_end=current_secs,
            cfg_scalp=current_scalp_cfg,
            cfg_resolved=current_resolved_cfg,
            now=now,
        )

        time.sleep(2.0)


if __name__ == "__main__":
    monitor_live_multi_setup_v1()
