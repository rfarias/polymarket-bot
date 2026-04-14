from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

from market.setup1_broker_executor_v4 import BrokerDecisionTicket
from market.setup1_order_manager_v2 import (
    OrderTicket,
    TwoLegPlan,
    STATUS_CANCELED,
    STATUS_CLOSED,
    STATUS_FILLED,
    STATUS_REJECTED,
)

TERMINAL_TICKET_STATUSES = {STATUS_FILLED, STATUS_CANCELED, STATUS_REJECTED, STATUS_CLOSED}
DEFAULT_STATE_FILE = "runtime/polymarket_executor_state_v1.json"


def _state_path(path: str | None = None) -> str:
    return os.path.abspath(path or os.getenv("POLY_STATE_FILE", DEFAULT_STATE_FILE))


def _serialize_plan(plan: TwoLegPlan) -> Dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "event_slug": plan.event_slug,
        "slot_name": plan.slot_name,
        "up_price": float(plan.up_price),
        "down_price": float(plan.down_price),
        "qty_per_leg": int(plan.qty_per_leg),
        "state": plan.state,
        "duplicate_blocked": bool(plan.duplicate_blocked),
        "logs": list(plan.logs),
        "tickets": {name: ticket.__dict__.copy() for name, ticket in (plan.tickets or {}).items()},
    }


def _deserialize_plan(data: Dict[str, Any]) -> TwoLegPlan:
    up_ticket = (data.get("tickets") or {}).get("up_entry") or {}
    down_ticket = (data.get("tickets") or {}).get("down_entry") or {}
    qty_per_leg = int(data.get("qty_per_leg") or up_ticket.get("requested_qty") or down_ticket.get("requested_qty") or 0)
    up_price = float(data.get("up_price") or up_ticket.get("price") or 0.0)
    down_price = float(data.get("down_price") or down_ticket.get("price") or 0.0)
    plan = TwoLegPlan(
        event_slug=str(data.get("event_slug") or ""),
        slot_name=str(data.get("slot_name") or "next_1"),
        up_price=up_price,
        down_price=down_price,
        qty_per_leg=qty_per_leg,
        plan_id=str(data.get("plan_id") or ""),
    )
    plan.state = str(data.get("state") or plan.state)
    plan.duplicate_blocked = bool(data.get("duplicate_blocked") or False)
    plan.logs = list(data.get("logs") or [])
    restored_tickets: Dict[str, OrderTicket] = {}
    for name, ticket_data in (data.get("tickets") or {}).items():
        restored_tickets[name] = OrderTicket(**ticket_data)
    plan.tickets = restored_tickets
    return plan


def _active_runtime_plan_ids(executor) -> List[str]:
    ids = []
    for runtime in executor.slots.values():
        if runtime.active_plan_id:
            ids.append(str(runtime.active_plan_id))
    return sorted(set(ids))


def reset_executor_state_v1(executor) -> Dict[str, Any]:
    for runtime in executor.slots.values():
        runtime.active_plan_id = None
        runtime.last_decision_key = None
        runtime.tickets = []
    executor.plan_broker_orders = {}
    executor.order_manager.active_plans = {}
    executor.order_manager.live_signatures = {}
    return {"ok": True, "reset": True}


def save_executor_state_v1(executor, path: str | None = None) -> Dict[str, Any]:
    state_file = _state_path(path)
    plan_ids = _active_runtime_plan_ids(executor)
    if not plan_ids:
        return clear_executor_state_v1(path=state_file, missing_ok=True)

    active_plans: Dict[str, Any] = {}
    for plan_id in plan_ids:
        if plan_id in executor.order_manager.active_plans:
            active_plans[plan_id] = _serialize_plan(executor.order_manager.get_plan(plan_id))

    payload = {
        "version": 1,
        "saved_at": time.time(),
        "plan_ids": plan_ids,
        "slots": {
            name: {
                "focus": bool(runtime.focus),
                "last_decision_key": runtime.last_decision_key,
                "active_plan_id": runtime.active_plan_id,
                "tickets": [ticket.as_dict() for ticket in runtime.tickets],
            }
            for name, runtime in executor.slots.items()
        },
        "plan_broker_orders": {plan_id: executor.plan_broker_orders.get(plan_id, {}) for plan_id in plan_ids},
        "active_plans": active_plans,
    }

    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return {
        "ok": True,
        "action": "saved",
        "path": state_file,
        "plan_ids": plan_ids,
        "plan_count": len(plan_ids),
    }


def clear_executor_state_v1(path: str | None = None, *, missing_ok: bool = True) -> Dict[str, Any]:
    state_file = _state_path(path)
    if os.path.exists(state_file):
        os.remove(state_file)
        return {"ok": True, "action": "cleared", "path": state_file}
    return {"ok": bool(missing_ok), "action": "already_missing", "path": state_file}


def flush_executor_state_v1(executor, path: str | None = None) -> Dict[str, Any]:
    if _active_runtime_plan_ids(executor):
        return save_executor_state_v1(executor, path=path)
    return clear_executor_state_v1(path=path, missing_ok=True)


def load_executor_state_v1(executor, path: str | None = None) -> Dict[str, Any]:
    state_file = _state_path(path)
    if not os.path.exists(state_file):
        return {"ok": True, "action": "not_found", "path": state_file, "restored_plan_ids": []}

    with open(state_file, "r", encoding="utf-8") as f:
        payload = json.load(f)

    reset_executor_state_v1(executor)

    for slot_name, slot_data in (payload.get("slots") or {}).items():
        runtime = executor.slots.get(slot_name)
        if runtime is None:
            continue
        runtime.focus = bool(slot_data.get("focus", runtime.focus))
        runtime.last_decision_key = slot_data.get("last_decision_key")
        runtime.active_plan_id = slot_data.get("active_plan_id")
        runtime.tickets = [BrokerDecisionTicket(**ticket) for ticket in (slot_data.get("tickets") or [])]

    restored_plan_ids: List[str] = []
    for plan_id, plan_data in (payload.get("active_plans") or {}).items():
        plan = _deserialize_plan(plan_data)
        executor.order_manager.active_plans[plan_id] = plan
        restored_plan_ids.append(plan_id)
        for ticket in plan.tickets.values():
            if ticket.status not in TERMINAL_TICKET_STATUSES and int(ticket.remaining_qty) > 0:
                executor.order_manager._register_ticket(ticket)
        runtime = executor.slots.get(plan.slot_name)
        if runtime is not None and not runtime.active_plan_id:
            runtime.active_plan_id = plan_id

    executor.plan_broker_orders = dict(payload.get("plan_broker_orders") or {})

    return {
        "ok": True,
        "action": "loaded",
        "path": state_file,
        "version": payload.get("version"),
        "restored_plan_ids": sorted(set(restored_plan_ids)),
        "restored_plan_count": len(set(restored_plan_ids)),
        "saved_at": payload.get("saved_at"),
    }
