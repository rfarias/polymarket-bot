from __future__ import annotations

from typing import Any, Dict, List, Tuple

from market.broker_reconciliation_v2 import reconcile_executor_with_broker_open_orders_v2
from market.setup1_order_manager_v2 import (
    PLAN_ABORTED,
    PLAN_DONE,
    PLAN_FORCE_CLOSED,
    PLAN_HEDGED,
    STATUS_CANCELED,
    STATUS_CLOSED,
    STATUS_FILLED,
    STATUS_REJECTED,
)

TERMINAL_TICKET_STATUSES = {STATUS_FILLED, STATUS_CANCELED, STATUS_REJECTED, STATUS_CLOSED}


def _runtime_for_plan(executor, plan_id: str):
    for runtime in executor.slots.values():
        if runtime.active_plan_id == plan_id:
            return runtime
    return None


def _post_sync_reconcile_entry_plan(executor, plan_id: str) -> List[str]:
    plan = executor.order_manager.get_plan(plan_id)
    logs: List[str] = []
    up = plan.tickets.get("up_entry")
    down = plan.tickets.get("down_entry")
    if not up or not down:
        return logs

    both_terminal = up.status in TERMINAL_TICKET_STATUSES and down.status in TERMINAL_TICKET_STATUSES
    if not both_terminal:
        return logs

    slot_name = plan.slot_name
    if up.filled_qty == 0 and down.filled_qty == 0:
        plan.state = PLAN_ABORTED
        logs.append(f"[PLAN] {slot_name}: broker sync terminal with no fills -> aborted")
        return logs

    if up.filled_qty == down.filled_qty and up.filled_qty > 0:
        if plan.state != PLAN_HEDGED:
            plan.state = PLAN_HEDGED
            logs.append(f"[PLAN] {slot_name}: broker sync terminal with balanced hedge qty={up.filled_qty}")
        return logs

    for event in executor.order_manager.force_close_plan(plan_id, "broker_sync_imbalance"):
        logs.append(f"[FORCE_CLOSE] {slot_name}: {event}")
    return logs


def sync_executor_from_broker_open_orders_v3(executor, broker_open_orders: List[Any]) -> Tuple[List[str], Dict[str, Any]]:
    logs: List[str] = []
    reconcile = reconcile_executor_with_broker_open_orders_v2(executor, broker_open_orders)

    touched_plan_ids = set()
    for row in (reconcile.get("tracked") or []):
        plan_id = row["plan_id"]
        touched_plan_ids.add(plan_id)
        leg = row["leg"]
        plan = executor.order_manager.get_plan(plan_id)
        ticket = plan.tickets.get(leg)
        if ticket is None:
            continue

        broker_filled = int(round(float(row.get("size_matched") or 0.0)))
        row_status = str(row.get("status") or "").lower()
        fill_delta = max(0, broker_filled - int(ticket.filled_qty))

        if fill_delta > 0:
            events = executor.order_manager.apply_fill(plan_id, leg, qty=fill_delta, price=float(row.get("price") or ticket.price))
            slot_name = plan.slot_name
            logs.append(f"[BROKER_SYNC] {slot_name}: {leg} fill_delta={fill_delta} broker_status={row_status}")
            logs.extend([f"[PLAN] {slot_name}: {e}" for e in events])

        if row_status == "canceled" and ticket.status not in TERMINAL_TICKET_STATUSES:
            events = ticket.cancel_remaining("broker_reconcile")
            slot_name = plan.slot_name
            if events:
                logs.append(f"[BROKER_SYNC] {slot_name}: {leg} canceled on broker")
                logs.extend([f"[PLAN] {slot_name}: {e}" for e in events])

        if row_status == "rejected" and ticket.status not in TERMINAL_TICKET_STATUSES:
            ticket.status = STATUS_REJECTED
            slot_name = plan.slot_name
            logs.append(f"[BROKER_SYNC] {slot_name}: {leg} rejected on broker")

    for plan_id in touched_plan_ids:
        logs.extend(_post_sync_reconcile_entry_plan(executor, plan_id))

    for plan_id in list(executor.order_manager.active_plans.keys()):
        plan = executor.order_manager.get_plan(plan_id)
        if plan.state in (PLAN_ABORTED, PLAN_DONE, PLAN_FORCE_CLOSED):
            runtime = _runtime_for_plan(executor, plan_id)
            if runtime and runtime.active_plan_id == plan_id:
                runtime.active_plan_id = None
                logs.append(f"[PLAN_END] {runtime.slot_name}: {plan.state}")
            executor.order_manager.close_plan(plan_id)

    return logs, reconcile
