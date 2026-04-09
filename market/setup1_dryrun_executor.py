from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
import time
import uuid

from market.setup1_policy import classify_signal, evaluate_entry_quality, plan_two_leg_order
from market.setup1_order_manager_v2 import Setup1OrderManagerV2, PLAN_DONE, PLAN_FORCE_CLOSED, PLAN_HEDGED, PLAN_EXIT_POSTED


@dataclass
class DecisionTicket:
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
class SlotRuntime:
    slot_name: str
    focus: bool = False
    last_decision_key: Optional[str] = None
    active_plan_id: Optional[str] = None
    tickets: List[DecisionTicket] = field(default_factory=list)

    def push_ticket(self, event_slug: str, signal: str, decision: str, reason: str, details: Optional[Dict[str, Any]]) -> DecisionTicket:
        ticket = DecisionTicket(
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


class Setup1DryRunExecutor:
    def __init__(self, min_stable_snapshots: int = 2, min_shares_per_leg: int = 5, default_tick_size: float = 0.01):
        self.min_stable_snapshots = min_stable_snapshots
        self.min_shares_per_leg = min_shares_per_leg
        self.default_tick_size = default_tick_size
        self.order_manager = Setup1OrderManagerV2()
        self.slots: Dict[str, SlotRuntime] = {
            "next_1": SlotRuntime(slot_name="next_1", focus=True),
            "next_2": SlotRuntime(slot_name="next_2", focus=False),
        }

    def _decision_key(self, decision: str, reason: str, details: Optional[Dict[str, Any]]) -> str:
        # Avoid log spam from volatile fields like secs_to_end.
        normalized = None
        if details:
            normalized = {
                "slot_name": details.get("slot_name"),
                "sum_asks": details.get("sum_asks"),
                "sum_bids": details.get("sum_bids"),
                "exit_gap_total": details.get("exit_gap_total"),
            }
        return f"{decision}|{reason}|{normalized}"

    def _emit_ticket_if_new(self, runtime: SlotRuntime, event_slug: str, signal: str, decision: str, reason: str, details: Optional[Dict[str, Any]]) -> List[str]:
        key = self._decision_key(decision, reason, details)
        if runtime.last_decision_key == key:
            return []
        runtime.last_decision_key = key
        ticket = runtime.push_ticket(event_slug=event_slug, signal=signal, decision=decision, reason=reason, details=details)
        lines = [f"[DECISION] {runtime.slot_name}: {decision}", f"[TICKET] {runtime.slot_name}: {ticket.as_dict()}"]
        return lines

    def evaluate_slot(self, *, slot_name: str, event_slug: str, signal: str, metrics: Optional[Dict[str, float]], secs_to_end: Optional[int]) -> List[str]:
        runtime = self.slots[slot_name]
        logs: List[str] = []
        plan = self.order_manager.get_plan(runtime.active_plan_id) if runtime.active_plan_id else None

        if plan is not None:
            logs.append(f"[PLAN_STATE] {slot_name}: {plan.state}")
            return logs

        if signal not in ("watching", "armed"):
            return logs

        ok, reason, details = evaluate_entry_quality(metrics, slot_name, secs_to_end)
        if not ok:
            logs.extend(self._emit_ticket_if_new(runtime, event_slug, signal, "blocked", reason, details))
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
            logs.extend(self._emit_ticket_if_new(runtime, event_slug, signal, "blocked", "duplicate_or_create_failed", details))
            logs.extend([f"[PLAN] {slot_name}: {e}" for e in events])
            return logs

        runtime.active_plan_id = created_plan.plan_id
        logs.extend(self._emit_ticket_if_new(runtime, event_slug, signal, "plan_created", "ok", details))
        logs.extend([f"[PLAN] {slot_name}: {e}" for e in events])
        return logs

    def process_market_tick(self, *, slot_name: str, event_slug: str, signal: str, metrics: Optional[Dict[str, float]], secs_to_end: Optional[int], deadline_trigger: Optional[int] = None) -> List[str]:
        runtime = self.slots[slot_name]
        logs: List[str] = []
        plan = self.order_manager.get_plan(runtime.active_plan_id) if runtime.active_plan_id else None

        if plan is None:
            logs.extend(self.evaluate_slot(slot_name=slot_name, event_slug=event_slug, signal=signal, metrics=metrics, secs_to_end=secs_to_end))
            plan = self.order_manager.get_plan(runtime.active_plan_id) if runtime.active_plan_id else None

        if plan is None or metrics is None:
            return logs

        # Entry fills on both legs
        up_entry = plan.tickets["up_entry"]
        down_entry = plan.tickets["down_entry"]

        if up_entry.remaining_qty > 0 and metrics["up_ask"] <= up_entry.price:
            fill_qty = min(1 if metrics["up_ask"] == up_entry.price else 2, up_entry.remaining_qty)
            for event in self.order_manager.apply_fill(plan.plan_id, "up_entry", fill_qty, metrics["up_ask"]):
                logs.append(f"[FILL] {slot_name}: {event}")

        if down_entry.remaining_qty > 0 and metrics["down_ask"] <= down_entry.price:
            fill_qty = min(1 if metrics["down_ask"] == down_entry.price else 2, down_entry.remaining_qty)
            for event in self.order_manager.apply_fill(plan.plan_id, "down_entry", fill_qty, metrics["down_ask"]):
                logs.append(f"[FILL] {slot_name}: {event}")

        plan = self.order_manager.get_plan(plan.plan_id)
        if plan.state == PLAN_HEDGED and "up_exit" not in plan.tickets:
            up_exit_price = round(plan.tickets["up_entry"].price + self.default_tick_size, 2)
            down_exit_price = round(plan.tickets["down_entry"].price + self.default_tick_size, 2)
            for event in self.order_manager.post_exit_orders(plan.plan_id, up_exit_price, down_exit_price):
                logs.append(f"[EXIT] {slot_name}: {event}")

        plan = self.order_manager.get_plan(plan.plan_id)
        if plan.state == PLAN_EXIT_POSTED:
            up_exit = plan.tickets["up_exit"]
            down_exit = plan.tickets["down_exit"]
            if up_exit.remaining_qty > 0 and metrics["up_bid"] >= up_exit.price:
                fill_qty = min(1 if metrics["up_bid"] == up_exit.price else 2, up_exit.remaining_qty)
                for event in self.order_manager.apply_fill(plan.plan_id, "up_exit", fill_qty, metrics["up_bid"]):
                    logs.append(f"[EXIT_FILL] {slot_name}: {event}")
            if down_exit.remaining_qty > 0 and metrics["down_bid"] >= down_exit.price:
                fill_qty = min(1 if metrics["down_bid"] == down_exit.price else 2, down_exit.remaining_qty)
                for event in self.order_manager.apply_fill(plan.plan_id, "down_exit", fill_qty, metrics["down_bid"]):
                    logs.append(f"[EXIT_FILL] {slot_name}: {event}")

        plan = self.order_manager.get_plan(plan.plan_id)
        if deadline_trigger is not None and secs_to_end is not None and secs_to_end <= deadline_trigger and plan.state not in (PLAN_DONE, PLAN_FORCE_CLOSED):
            for event in self.order_manager.on_deadline(plan.plan_id):
                logs.append(f"[PLAN] {slot_name}: {event}")
            for event in self.order_manager.force_close_plan(plan.plan_id, "time_trigger", metrics.get("up_bid"), metrics.get("down_bid")):
                logs.append(f"[FORCE_CLOSE] {slot_name}: {event}")
            plan = self.order_manager.get_plan(plan.plan_id)

        if plan.state in (PLAN_DONE, PLAN_FORCE_CLOSED):
            runtime.active_plan_id = None
            self.order_manager.close_plan(plan.plan_id)
            logs.append(f"[PLAN_END] {slot_name}: {plan.state}")

        return logs

    def snapshot(self) -> Dict[str, Any]:
        return {
            "slots": {
                name: {
                    "focus": runtime.focus,
                    "last_decision_key": runtime.last_decision_key,
                    "active_plan_id": runtime.active_plan_id,
                    "tickets": [t.as_dict() for t in runtime.tickets],
                }
                for name, runtime in self.slots.items()
            }
        }
