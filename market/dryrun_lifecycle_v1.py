from __future__ import annotations

from typing import Any, Dict, List, Optional

from market.broker_types import BrokerOrderRequest
from market.setup1_order_manager_v2 import PLAN_DONE, PLAN_EXIT_POSTED, PLAN_HEDGED, PLAN_WORKING


def _slot_snap(slot_state: Dict[str, Any], slot_name: str) -> Dict[str, Any]:
    slot = slot_state.get(slot_name)
    if not slot:
        return {"up": None, "down": None}
    up = None
    down = None
    for item in slot.get("books") or []:
        outcome = str(item.get("outcome") or "").lower()
        if outcome == "up":
            up = item
        elif outcome == "down":
            down = item
    return {"up": up, "down": down}


def _sync_payload_from_broker(executor, broker, plan_id: str, leg: str) -> None:
    payload = (executor.plan_broker_orders.get(plan_id) or {}).get(leg)
    if not payload:
        return
    order_id = (payload.get("order") or {}).get("order_id")
    if not order_id:
        return
    order = broker.get_order(order_id)
    if not order:
        return
    payload["order"] = order.as_dict()


def _fill_broker_order_if_crossed(broker, order_id: str, fill_qty: float) -> bool:
    order = broker.get_order(order_id)
    if not order or order.status not in ("open", "partial"):
        return False
    remaining = order.remaining_size
    if remaining <= 0:
        return False
    executable = min(float(fill_qty), remaining)
    order.size_matched += executable
    if order.remaining_size <= 0:
        order.status = "filled"
    else:
        order.status = "partial"
    return True


def advance_public_dryrun_lifecycle_v1(executor, broker, slot_state: Dict[str, Any]) -> List[str]:
    logs: List[str] = []

    for slot_name in ("next_1", "next_2"):
        runtime = executor.slots[slot_name]
        plan_id = runtime.active_plan_id
        if not plan_id:
            continue

        plan = executor.order_manager.get_plan(plan_id)
        snap = _slot_snap(slot_state, slot_name)
        up = snap.get("up")
        down = snap.get("down")
        if not up or not down:
            continue

        if plan.state == PLAN_WORKING:
            for leg, market_side in (("up_entry", up), ("down_entry", down)):
                payload = (executor.plan_broker_orders.get(plan_id) or {}).get(leg)
                if not payload:
                    continue
                order_id = (payload.get("order") or {}).get("order_id")
                if not order_id:
                    continue
                order = broker.get_order(order_id)
                if not order or order.status not in ("open", "partial"):
                    continue
                executable_buy = market_side.get("executable_buy")
                if executable_buy is None:
                    continue
                if float(order.price) >= float(executable_buy):
                    did_fill = _fill_broker_order_if_crossed(broker, order_id, order.remaining_size)
                    if did_fill:
                        qty = int(round(order.original_size))
                        events = executor.order_manager.apply_fill(plan_id, leg, qty=qty, price=float(executable_buy))
                        logs.append(f"[SIM_FILL] {slot_name}: {leg} filled at {executable_buy}")
                        logs.extend([f"[PLAN] {slot_name}: {e}" for e in events])
                        _sync_payload_from_broker(executor, broker, plan_id, leg)

            plan = executor.order_manager.get_plan(plan_id)
            if plan.state == PLAN_HEDGED and "up_exit" not in plan.tickets and "down_exit" not in plan.tickets:
                up_sell = up.get("executable_sell")
                down_sell = down.get("executable_sell")
                if up_sell is not None and down_sell is not None:
                    events = executor.order_manager.post_exit_orders(plan_id, up_exit_price=float(up_sell), down_exit_price=float(down_sell))
                    logs.extend([f"[PLAN] {slot_name}: {e}" for e in events])
                    qty = min(plan.tickets["up_entry"].filled_qty, plan.tickets["down_entry"].filled_qty)
                    up_req = BrokerOrderRequest(
                        token_id=f"{plan.event_slug}:UP",
                        side="SELL",
                        price=float(up_sell),
                        size=float(qty),
                        market_slug=plan.event_slug,
                        outcome="UP",
                        client_order_key=f"{plan.plan_id}:up_exit",
                    )
                    down_req = BrokerOrderRequest(
                        token_id=f"{plan.event_slug}:DOWN",
                        side="SELL",
                        price=float(down_sell),
                        size=float(qty),
                        market_slug=plan.event_slug,
                        outcome="DOWN",
                        client_order_key=f"{plan.plan_id}:down_exit",
                    )
                    up_order = broker.place_limit_order(up_req)
                    down_order = broker.place_limit_order(down_req)
                    executor.plan_broker_orders.setdefault(plan_id, {})["up_exit"] = {"mode": broker.mode, "request": up_req.as_dict(), "order": up_order.as_dict()}
                    executor.plan_broker_orders.setdefault(plan_id, {})["down_exit"] = {"mode": broker.mode, "request": down_req.as_dict(), "order": down_order.as_dict()}
                    logs.append(f"[BROKER_ORDER] {slot_name}: up_exit -> {up_order.as_dict()}")
                    logs.append(f"[BROKER_ORDER] {slot_name}: down_exit -> {down_order.as_dict()}")

        plan = executor.order_manager.get_plan(plan_id)
        if plan.state == PLAN_EXIT_POSTED:
            for leg, market_side in (("up_exit", up), ("down_exit", down)):
                payload = (executor.plan_broker_orders.get(plan_id) or {}).get(leg)
                if not payload:
                    continue
                order_id = (payload.get("order") or {}).get("order_id")
                if not order_id:
                    continue
                order = broker.get_order(order_id)
                if not order or order.status not in ("open", "partial"):
                    continue
                executable_sell = market_side.get("executable_sell")
                if executable_sell is None:
                    continue
                if float(order.price) <= float(executable_sell):
                    did_fill = _fill_broker_order_if_crossed(broker, order_id, order.remaining_size)
                    if did_fill:
                        qty = int(round(order.original_size))
                        events = executor.order_manager.apply_fill(plan_id, leg, qty=qty, price=float(executable_sell))
                        logs.append(f"[SIM_FILL] {slot_name}: {leg} filled at {executable_sell}")
                        logs.extend([f"[PLAN] {slot_name}: {e}" for e in events])
                        _sync_payload_from_broker(executor, broker, plan_id, leg)

        plan = executor.order_manager.get_plan(plan_id)
        if plan.state == PLAN_DONE:
            runtime.active_plan_id = None
            executor.order_manager.close_plan(plan_id)
            logs.append(f"[PLAN_END] {slot_name}: done")

    return logs
