from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
import time
import uuid

from market.setup1_policy import evaluate_entry_quality, plan_two_leg_order
from market.setup1_order_manager_v2 import (
    Setup1OrderManagerV2,
    PLAN_ABORTED,
    PLAN_DONE,
    PLAN_FORCE_CLOSED,
)
from market.broker_interface import BrokerInterface
from market.broker_types import BrokerOrderRequest


@dataclass
class BrokerDecisionTicket:
    ticket_id: str
    slot_name: str
    event_slug: str
    signal: str
    decision: str
    reason: str
    created_at: float
    details: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BrokerSlotRuntime:
    slot_name: str
    focus: bool = False
    last_decision_key: Optional[str] = None
    active_plan_id: Optional[str] = None
    tickets: List[BrokerDecisionTicket] = field(default_factory=list)

    def push_ticket(self, *, event_slug: str, signal: str, decision: str, reason: str, details: Optional[Dict[str, Any]]) -> BrokerDecisionTicket:
        ticket = BrokerDecisionTicket(
            ticket_id=str(uuid.uuid4()),
            slot_name=self.slot_name,
            event_slug=event_slug,
            signal=signal,
            decision=decision,
            reason=reason,
            created_at=time.time(),
            details=details,
        )
        self.tickets.append(ticket)
        return ticket


class Setup1BrokerExecutorV2:
    def __init__(
        self,
        *,
        broker: BrokerInterface,
        shadow_only: bool = True,
        min_shares_per_leg: int = 5,
    ):
        self.broker = broker
        self.shadow_only = shadow_only
        self.min_shares_per_leg = min_shares_per_leg
        self.order_manager = Setup1OrderManagerV2()
        self.slots: Dict[str, BrokerSlotRuntime] = {
            "next_1": BrokerSlotRuntime(slot_name="next_1", focus=True),
            "next_2": BrokerSlotRuntime(slot_name="next_2", focus=False),
        }
        self.plan_broker_orders: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _decision_key(self, decision: str, reason: str, details: Optional[Dict[str, Any]]) -> str:
        normalized = None
        if details:
            normalized = {
                "slot_name": details.get("slot_name"),
                "sum_asks": details.get("sum_asks"),
                "sum_bids": details.get("sum_bids"),
                "exit_gap_total": details.get("exit_gap_total"),
            }
        return f"{decision}|{reason}|{normalized}"

    def _emit_ticket_if_new(self, runtime: BrokerSlotRuntime, *, event_slug: str, signal: str, decision: str, reason: str, details: Optional[Dict[str, Any]]) -> List[str]:
        key = self._decision_key(decision, reason, details)
        if runtime.last_decision_key == key:
            return []
        runtime.last_decision_key = key
        ticket = runtime.push_ticket(event_slug=event_slug, signal=signal, decision=decision, reason=reason, details=details)
        return [f"[DECISION] {runtime.slot_name}: {decision}", f"[TICKET] {runtime.slot_name}: {ticket.as_dict()}"]

    def _register_broker_order(self, plan_id: str, leg: str, payload: Dict[str, Any]) -> None:
        self.plan_broker_orders.setdefault(plan_id, {})[leg] = payload

    def _place_or_shadow_order(self, *, plan_id: str, slot_name: str, req: BrokerOrderRequest, leg: str) -> List[str]:
        if self.shadow_only:
            self._register_broker_order(plan_id, leg, {"mode": "shadow", "request": req.as_dict(), "status": "shadow_posted"})
            return [f"[SHADOW_ORDER] {slot_name}: {leg} -> {req.as_dict()}"]
        order = self.broker.place_limit_order(req)
        self._register_broker_order(plan_id, leg, {"mode": self.broker.mode, "request": req.as_dict(), "order": order.as_dict()})
        return [f"[BROKER_ORDER] {slot_name}: {leg} -> {order.as_dict()}"]

    def evaluate_slot(self, *, slot_name: str, event_slug: str, signal: str, metrics: Optional[Dict[str, float]], secs_to_end: Optional[int]) -> List[str]:
        runtime = self.slots[slot_name]
        logs: List[str] = []
        plan = self.order_manager.get_plan(runtime.active_plan_id) if runtime.active_plan_id else None
        if plan is not None:
            return [f"[PLAN_STATE] {slot_name}: {plan.state}"]
        if signal not in ("watching", "armed"):
            return logs

        ok, reason, details = evaluate_entry_quality(metrics, slot_name, secs_to_end)
        if not ok:
            logs.extend(self._emit_ticket_if_new(runtime, event_slug=event_slug, signal=signal, decision="blocked", reason=reason, details=details))
            return logs

        order_plan = plan_two_leg_order(metrics, self.min_shares_per_leg)
        created_plan, events = self.order_manager.create_two_leg_plan(
            event_slug=event_slug,
            slot_name=slot_name,
            up_price=order_plan["up_limit_price"],
            down_price=order_plan["down_limit_price"],
            qty_per_leg=self.min_shares_per_leg,
        )
        if created_plan is None:
            logs.extend(self._emit_ticket_if_new(runtime, event_slug=event_slug, signal=signal, decision="blocked", reason="duplicate_or_create_failed", details=details))
            logs.extend([f"[PLAN] {slot_name}: {e}" for e in events])
            return logs

        runtime.active_plan_id = created_plan.plan_id
        logs.extend(self._emit_ticket_if_new(runtime, event_slug=event_slug, signal=signal, decision="plan_created", reason="ok", details=details))
        logs.extend([f"[PLAN] {slot_name}: {e}" for e in events])

        up_req = BrokerOrderRequest(
            token_id=f"{event_slug}:UP",
            side="BUY",
            price=float(order_plan["up_limit_price"]),
            size=float(self.min_shares_per_leg),
            market_slug=event_slug,
            outcome="UP",
            client_order_key=f"{created_plan.plan_id}:up_entry",
        )
        down_req = BrokerOrderRequest(
            token_id=f"{event_slug}:DOWN",
            side="BUY",
            price=float(order_plan["down_limit_price"]),
            size=float(self.min_shares_per_leg),
            market_slug=event_slug,
            outcome="DOWN",
            client_order_key=f"{created_plan.plan_id}:down_entry",
        )
        logs.extend(self._place_or_shadow_order(plan_id=created_plan.plan_id, slot_name=slot_name, req=up_req, leg="up_entry"))
        logs.extend(self._place_or_shadow_order(plan_id=created_plan.plan_id, slot_name=slot_name, req=down_req, leg="down_entry"))
        return logs

    def on_deadline(self, *, slot_name: str, metrics: Optional[Dict[str, float]] = None) -> List[str]:
        runtime = self.slots[slot_name]
        if not runtime.active_plan_id:
            return []
        plan_id = runtime.active_plan_id
        logs: List[str] = []

        broker_orders = self.plan_broker_orders.get(plan_id, {})
        for leg, payload in broker_orders.items():
            if self.shadow_only:
                logs.append(f"[SHADOW_CANCEL] {slot_name}: {leg} -> {payload.get('request')}")
            else:
                order_id = payload.get("order", {}).get("order_id")
                if order_id:
                    resp = self.broker.cancel_order(order_id)
                    logs.append(f"[BROKER_CANCEL] {slot_name}: {leg} -> {resp}")

        for event in self.order_manager.on_deadline(plan_id):
            logs.append(f"[PLAN] {slot_name}: {event}")

        plan = self.order_manager.get_plan(plan_id)
        if plan.state == PLAN_FORCE_CLOSED and metrics:
            for event in self.order_manager.force_close_plan(plan_id, "time_trigger", metrics.get("up_bid"), metrics.get("down_bid")):
                logs.append(f"[FORCE_CLOSE] {slot_name}: {event}")
            plan = self.order_manager.get_plan(plan_id)

        if plan.state in (PLAN_ABORTED, PLAN_DONE, PLAN_FORCE_CLOSED):
            runtime.active_plan_id = None
            self.order_manager.close_plan(plan_id)
            logs.append(f"[PLAN_END] {slot_name}: {plan.state}")
        return logs

    def process_market_tick(self, *, slot_name: str, event_slug: str, signal: str, metrics: Optional[Dict[str, float]], secs_to_end: Optional[int], deadline_trigger: Optional[int] = None) -> List[str]:
        logs = self.evaluate_slot(slot_name=slot_name, event_slug=event_slug, signal=signal, metrics=metrics, secs_to_end=secs_to_end)
        if deadline_trigger is not None and secs_to_end is not None and secs_to_end <= deadline_trigger:
            logs.extend(self.on_deadline(slot_name=slot_name, metrics=metrics))
        return logs

    def snapshot(self) -> Dict[str, Any]:
        return {
            "broker": {
                "mode": self.broker.mode,
                "shadow_only": self.shadow_only,
            },
            "slots": {
                name: {
                    "focus": runtime.focus,
                    "last_decision_key": runtime.last_decision_key,
                    "active_plan_id": runtime.active_plan_id,
                    "tickets": [t.as_dict() for t in runtime.tickets],
                }
                for name, runtime in self.slots.items()
            },
            "plan_broker_orders": self.plan_broker_orders,
        }
