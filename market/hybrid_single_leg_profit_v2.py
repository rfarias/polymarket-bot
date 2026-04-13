from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from market.setup1_order_manager_v2 import PLAN_WORKING, PLAN_FORCE_CLOSED
from market.setup1_policy import DEFAULT_TICK_SIZE


def _mark_shadow_canceled(executor, plan_id: str, leg: str) -> Optional[Dict]:
    payload = (executor.plan_broker_orders.get(plan_id) or {}).get(leg)
    if not payload:
        return None
    payload["status"] = "shadow_canceled"
    return payload.get("request")


def _cancel_pending_leg(executor, slot_name: str, plan_id: str, leg: str) -> List[str]:
    logs: List[str] = []
    payload = (executor.plan_broker_orders.get(plan_id) or {}).get(leg)
    if not payload:
        return logs
    if executor.shadow_only:
        req = _mark_shadow_canceled(executor, plan_id, leg)
        logs.append(f"[SHADOW_CANCEL] {slot_name}: {leg} -> {req}")
        return logs
    order_id = (payload.get("order") or {}).get("order_id")
    if order_id:
        resp = executor.broker.cancel_order(order_id)
        payload["cancel_response"] = resp
        if payload.get("order"):
            payload["order"]["status"] = "canceled"
            payload["order"]["remaining_size"] = 0.0
        logs.append(f"[BROKER_CANCEL] {slot_name}: {leg} -> {resp}")
    return logs


def _extract_last_fill_price(ticket, payload: Optional[Dict]) -> float:
    order = (payload or {}).get("order") or {}
    try:
        if float(order.get("size_matched") or 0.0) > 0 and order.get("price") is not None:
            return float(order.get("price"))
    except Exception:
        pass
    for note in reversed(ticket.notes or []):
        if isinstance(note, str) and note.startswith("last_fill_price="):
            try:
                return float(note.split("=", 1)[1])
            except Exception:
                break
    return float(ticket.price)


def _single_leg_profit_candidate(executor, plan, metrics: Dict[str, float], tick_size: float) -> Optional[Tuple[str, str, float, float]]:
    up = plan.tickets.get("up_entry")
    down = plan.tickets.get("down_entry")
    if not up or not down:
        return None

    up_payload = (executor.plan_broker_orders.get(plan.plan_id) or {}).get("up_entry")
    down_payload = (executor.plan_broker_orders.get(plan.plan_id) or {}).get("down_entry")

    if up.filled_qty > 0 and down.filled_qty == 0:
        entry_price = _extract_last_fill_price(up, up_payload)
        exit_price = float(metrics.get("up_bid") or 0.0)
        if exit_price >= round(entry_price + tick_size, 4):
            return ("up_entry", "down_entry", entry_price, exit_price)
    if down.filled_qty > 0 and up.filled_qty == 0:
        entry_price = _extract_last_fill_price(down, down_payload)
        exit_price = float(metrics.get("down_bid") or 0.0)
        if exit_price >= round(entry_price + tick_size, 4):
            return ("down_entry", "up_entry", entry_price, exit_price)
    return None


def maybe_take_single_leg_profit_v2(executor, *, slot_name: str, metrics: Optional[Dict[str, float]], tick_size: float = DEFAULT_TICK_SIZE) -> List[str]:
    runtime = executor.slots.get(slot_name)
    if not runtime or not runtime.active_plan_id or not metrics:
        return []

    plan_id = runtime.active_plan_id
    plan = executor.order_manager.get_plan(plan_id)
    if plan.state != PLAN_WORKING:
        return []

    candidate = _single_leg_profit_candidate(executor, plan, metrics, tick_size)
    if not candidate:
        return []

    filled_leg, pending_leg, entry_price, exit_price = candidate
    logs: List[str] = [
        f"[SINGLE_LEG_TP] {slot_name}: trigger {filled_leg} entry={entry_price} exit={round(exit_price, 4)} tick={tick_size}"
    ]
    logs.extend(_cancel_pending_leg(executor, slot_name, plan_id, pending_leg))

    for event in executor.order_manager.force_close_plan(
        plan_id,
        "single_leg_profit",
        metrics.get("up_bid"),
        metrics.get("down_bid"),
    ):
        logs.append(f"[SINGLE_LEG_TP] {slot_name}: {event}")

    plan = executor.order_manager.get_plan(plan_id)
    if plan.state == PLAN_FORCE_CLOSED:
        runtime.active_plan_id = None
        executor.order_manager.close_plan(plan_id)
        logs.append(f"[PLAN_END] {slot_name}: single_leg_profit")
    return logs
