from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from market.broker_types import BrokerOrderRequest
from market.setup1_order_manager_v2 import (
    OrderTicket,
    PLAN_ABORTED,
    PLAN_DONE,
    PLAN_EXIT_POSTED,
    PLAN_FORCE_CLOSED,
    PLAN_HEDGED,
    PLAN_WORKING,
    STATUS_CANCELED,
    STATUS_CLOSED,
    STATUS_FILLED,
    STATUS_REJECTED,
)
from market.setup1_policy import DEFAULT_TICK_SIZE

TERMINAL_TICKET_STATUSES = {STATUS_FILLED, STATUS_CANCELED, STATUS_REJECTED, STATUS_CLOSED}


def _exit_leg_for_entry(entry_leg: str) -> str:
    return "up_exit" if entry_leg.startswith("up") else "down_exit"


def _runtime(executor, slot_name: str):
    return executor.slots.get(slot_name)


def _plan(executor, slot_name: str):
    runtime = _runtime(executor, slot_name)
    if not runtime or not runtime.active_plan_id:
        return None
    return executor.order_manager.get_plan(runtime.active_plan_id)


def _entry_payload(executor, plan_id: str, entry_leg: str) -> Optional[Dict]:
    return (executor.plan_broker_orders.get(plan_id) or {}).get(entry_leg)


def _entry_token_id(executor, plan_id: str, entry_leg: str) -> Optional[str]:
    payload = _entry_payload(executor, plan_id, entry_leg) or {}
    req = payload.get("request") or {}
    order = payload.get("order") or {}
    return req.get("token_id") or order.get("token_id")


def _entry_outcome(executor, plan_id: str, entry_leg: str) -> Optional[str]:
    payload = _entry_payload(executor, plan_id, entry_leg) or {}
    req = payload.get("request") or {}
    order = payload.get("order") or {}
    return req.get("outcome") or order.get("outcome")


def _extract_fill_price(ticket, payload: Optional[Dict]) -> float:
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


def _cancel_entry_leg_real(executor, slot_name: str, plan_id: str, leg: str, reason: str) -> List[str]:
    logs: List[str] = []
    payload = _entry_payload(executor, plan_id, leg)
    plan = executor.order_manager.get_plan(plan_id)
    ticket = plan.tickets.get(leg)
    if not payload or not ticket:
        return logs
    if ticket.status in TERMINAL_TICKET_STATUSES:
        return logs
    order_id = (payload.get("order") or {}).get("order_id")
    if order_id:
        resp = executor.broker.cancel_order(order_id)
        payload["cancel_response"] = resp
        if payload.get("order"):
            payload["order"]["status"] = "canceled"
            payload["order"]["remaining_size"] = 0.0
        logs.append(f"[BROKER_CANCEL] {slot_name}: {leg} -> {resp}")
    for event in ticket.cancel_remaining(reason):
        logs.append(f"[PLAN] {slot_name}: {event}")
    return logs


def _ensure_exit_ticket(executor, plan_id: str, entry_leg: str, exit_price: float) -> Tuple[str, OrderTicket, int]:
    plan = executor.order_manager.get_plan(plan_id)
    entry_ticket = plan.tickets[entry_leg]
    exit_leg = _exit_leg_for_entry(entry_leg)
    qty = int(entry_ticket.filled_qty)
    if exit_leg not in plan.tickets:
        ticket = OrderTicket(
            plan_id=plan.plan_id,
            event_slug=plan.event_slug,
            slot_name=plan.slot_name,
            leg=exit_leg,
            outcome=entry_ticket.outcome,
            side="sell",
            price=float(exit_price),
            requested_qty=qty,
        )
        plan.tickets[exit_leg] = ticket
        executor.order_manager._register_ticket(ticket)
    else:
        ticket = plan.tickets[exit_leg]
        ticket.price = float(exit_price)
    return exit_leg, ticket, qty


def _post_real_exit_order(executor, slot_name: str, plan_id: str, entry_leg: str, exit_price: float, reason: str) -> List[str]:
    plan = executor.order_manager.get_plan(plan_id)
    logs: List[str] = []
    exit_leg, exit_ticket, qty = _ensure_exit_ticket(executor, plan_id, entry_leg, exit_price)
    if qty <= 0:
        return logs
    if (executor.plan_broker_orders.get(plan_id) or {}).get(exit_leg):
        return logs

    token_id = _entry_token_id(executor, plan_id, entry_leg)
    outcome = _entry_outcome(executor, plan_id, entry_leg)
    if not token_id or not outcome:
        logs.append(f"[BROKER_EXIT_ORDER_ERROR] {slot_name}: missing token_id/outcome for {entry_leg}")
        return logs

    req = BrokerOrderRequest(
        token_id=str(token_id),
        side="SELL",
        price=float(exit_price),
        size=float(qty),
        market_slug=plan.event_slug,
        outcome=str(outcome),
        client_order_key=f"{plan.plan_id}:{exit_leg}",
    )
    order = executor.broker.place_limit_order(req)
    executor.plan_broker_orders.setdefault(plan_id, {})[exit_leg] = {
        "mode": executor.broker.mode,
        "request": req.as_dict(),
        "order": order.as_dict(),
        "exit_reason": reason,
    }
    plan.state = PLAN_EXIT_POSTED
    logs.append(f"[BROKER_EXIT_ORDER] {slot_name}: {exit_leg} -> {order.as_dict()}")
    return logs


def maybe_take_single_leg_profit_real_v2(executor, *, slot_name: str, metrics: Optional[Dict[str, float]], tick_size: float = DEFAULT_TICK_SIZE) -> List[str]:
    plan = _plan(executor, slot_name)
    if plan is None or not metrics:
        return []
    if plan.state != PLAN_WORKING:
        return []

    up = plan.tickets.get("up_entry")
    down = plan.tickets.get("down_entry")
    if not up or not down:
        return []

    broker_orders = executor.plan_broker_orders.get(plan.plan_id) or {}
    if up.filled_qty > 0 and down.filled_qty == 0:
        entry_price = _extract_fill_price(up, broker_orders.get("up_entry"))
        exit_price = float(metrics.get("up_bid") or 0.0)
        if exit_price >= round(entry_price + tick_size, 4):
            logs = [f"[SINGLE_LEG_TP] {slot_name}: trigger up_entry entry={entry_price} exit={exit_price} tick={tick_size}"]
            logs.extend(_cancel_entry_leg_real(executor, slot_name, plan.plan_id, "down_entry", "single_leg_profit"))
            logs.extend(_post_real_exit_order(executor, slot_name, plan.plan_id, "up_entry", exit_price, "single_leg_profit"))
            return logs
    if down.filled_qty > 0 and up.filled_qty == 0:
        entry_price = _extract_fill_price(down, broker_orders.get("down_entry"))
        exit_price = float(metrics.get("down_bid") or 0.0)
        if exit_price >= round(entry_price + tick_size, 4):
            logs = [f"[SINGLE_LEG_TP] {slot_name}: trigger down_entry entry={entry_price} exit={exit_price} tick={tick_size}"]
            logs.extend(_cancel_entry_leg_real(executor, slot_name, plan.plan_id, "up_entry", "single_leg_profit"))
            logs.extend(_post_real_exit_order(executor, slot_name, plan.plan_id, "down_entry", exit_price, "single_leg_profit"))
            return logs
    return []


def maybe_post_force_close_exits_v2(executor, *, slot_name: str, metrics: Optional[Dict[str, float]]) -> List[str]:
    plan = _plan(executor, slot_name)
    if plan is None or not metrics:
        return []
    if plan.state != PLAN_FORCE_CLOSED:
        return []
    logs: List[str] = []
    up = plan.tickets.get("up_entry")
    down = plan.tickets.get("down_entry")
    if up and up.filled_qty > 0:
        logs.extend(_post_real_exit_order(executor, slot_name, plan.plan_id, "up_entry", float(metrics.get("up_bid") or 0.0), "force_close"))
    if down and down.filled_qty > 0:
        logs.extend(_post_real_exit_order(executor, slot_name, plan.plan_id, "down_entry", float(metrics.get("down_bid") or 0.0), "force_close"))
    return logs


def maybe_post_balanced_exit_orders_v2(executor, *, slot_name: str, metrics: Optional[Dict[str, float]]) -> List[str]:
    plan = _plan(executor, slot_name)
    if plan is None:
        return []
    if plan.state != PLAN_HEDGED:
        return []
    marker = "hedged_hold_resolution_logged"
    if marker in (plan.logs or []):
        return []
    plan.logs.append(marker)
    return [f"[PLAN] {slot_name}: balanced hedge confirmed -> hold until resolution (no exit orders)"]


def handle_deadline_real_v2(executor, *, slot_name: str, secs_to_end: Optional[int], deadline_trigger: Optional[int], metrics: Optional[Dict[str, float]]) -> List[str]:
    if deadline_trigger is None or secs_to_end is None or secs_to_end > deadline_trigger:
        return []
    plan = _plan(executor, slot_name)
    if plan is None:
        return []
    logs: List[str] = []

    logs.extend(_cancel_entry_leg_real(executor, slot_name, plan.plan_id, "up_entry", "deadline"))
    logs.extend(_cancel_entry_leg_real(executor, slot_name, plan.plan_id, "down_entry", "deadline"))

    up = plan.tickets.get("up_entry")
    down = plan.tickets.get("down_entry")
    if up.filled_qty == 0 and down.filled_qty == 0:
        plan.state = PLAN_ABORTED
        logs.append(f"[PLAN] {slot_name}: deadline with no fills -> aborted cleanly")
        return logs
    if up.filled_qty == down.filled_qty and up.filled_qty > 0:
        plan.state = PLAN_HEDGED
        logs.append(f"[PLAN] {slot_name}: deadline with balanced filled hedge qty={up.filled_qty} -> hold to resolution")
        logs.extend(maybe_post_balanced_exit_orders_v2(executor, slot_name=slot_name, metrics=metrics))
        return logs
    plan.state = PLAN_FORCE_CLOSED
    logs.append(f"[PLAN] {slot_name}: deadline with imbalance up={up.filled_qty} down={down.filled_qty} -> post real exits")
    logs.extend(maybe_post_force_close_exits_v2(executor, slot_name=slot_name, metrics=metrics))
    return logs


def cleanup_terminal_plan_v2(executor, *, slot_name: str) -> List[str]:
    plan = _plan(executor, slot_name)
    if plan is None:
        return []
    logs: List[str] = []
    if plan.state in (PLAN_ABORTED, PLAN_DONE):
        runtime = _runtime(executor, slot_name)
        if runtime and runtime.active_plan_id == plan.plan_id:
            runtime.active_plan_id = None
            logs.append(f"[PLAN_END] {slot_name}: {plan.state}")
        executor.order_manager.close_plan(plan.plan_id)
    return logs
