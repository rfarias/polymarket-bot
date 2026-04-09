from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple
import uuid

STATUS_OPEN = "open"
STATUS_PARTIAL = "partial"
STATUS_FILLED = "filled"
STATUS_CANCELED = "canceled"
STATUS_REJECTED = "rejected"
STATUS_CLOSED = "closed"

PLAN_PENDING = "pending"
PLAN_WORKING = "working"
PLAN_HEDGED = "hedged"
PLAN_EXIT_POSTED = "exit_posted"
PLAN_DONE = "done"
PLAN_ABORTED = "aborted"
PLAN_FORCE_CLOSED = "force_closed"


@dataclass
class OrderTicket:
    plan_id: str
    event_slug: str
    slot_name: str
    leg: str
    outcome: str
    side: str
    price: float
    requested_qty: int
    filled_qty: int = 0
    remaining_qty: int = 0
    status: str = STATUS_OPEN
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    notes: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.remaining_qty == 0:
            self.remaining_qty = self.requested_qty

    def signature(self) -> Tuple[str, str, str, float, int]:
        return (self.event_slug, self.leg, self.side, float(self.price), int(self.requested_qty))

    def apply_fill(self, qty: int, price: Optional[float] = None) -> List[str]:
        events: List[str] = []
        if self.status in (STATUS_CANCELED, STATUS_REJECTED, STATUS_CLOSED, STATUS_FILLED):
            return events
        if qty <= 0:
            return events
        executable = min(qty, self.remaining_qty)
        if executable <= 0:
            return events
        self.filled_qty += executable
        self.remaining_qty -= executable
        if self.remaining_qty == 0:
            self.status = STATUS_FILLED
            events.append(f"{self.leg} fully filled {self.filled_qty}/{self.requested_qty}")
        else:
            self.status = STATUS_PARTIAL
            events.append(f"{self.leg} partially filled {self.filled_qty}/{self.requested_qty}")
        if price is not None:
            self.notes.append(f"last_fill_price={price}")
        return events

    def cancel_remaining(self, reason: str) -> List[str]:
        if self.status in (STATUS_FILLED, STATUS_CANCELED, STATUS_REJECTED, STATUS_CLOSED):
            return []
        self.status = STATUS_CANCELED
        self.notes.append(f"cancel_reason={reason}")
        return [f"{self.leg} remaining canceled | reason={reason}"]

    def close_manually(self, reason: str, price: Optional[float] = None) -> List[str]:
        if self.status == STATUS_CLOSED:
            return []
        self.status = STATUS_CLOSED
        if price is not None:
            self.notes.append(f"manual_close_price={price}")
        self.notes.append(f"manual_close_reason={reason}")
        return [f"{self.leg} manually closed | reason={reason}"]


@dataclass
class TwoLegPlan:
    event_slug: str
    slot_name: str
    up_price: float
    down_price: float
    qty_per_leg: int
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: str = PLAN_PENDING
    tickets: Dict[str, OrderTicket] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)
    duplicate_blocked: bool = False

    def __post_init__(self):
        self.tickets["up_entry"] = OrderTicket(
            plan_id=self.plan_id,
            event_slug=self.event_slug,
            slot_name=self.slot_name,
            leg="up_entry",
            outcome="up",
            side="buy",
            price=float(self.up_price),
            requested_qty=int(self.qty_per_leg),
        )
        self.tickets["down_entry"] = OrderTicket(
            plan_id=self.plan_id,
            event_slug=self.event_slug,
            slot_name=self.slot_name,
            leg="down_entry",
            outcome="down",
            side="buy",
            price=float(self.down_price),
            requested_qty=int(self.qty_per_leg),
        )
        self.logs.append(f"plan created | qty_per_leg={self.qty_per_leg}")

    def as_dict(self) -> Dict:
        return {
            "plan_id": self.plan_id,
            "event_slug": self.event_slug,
            "slot_name": self.slot_name,
            "state": self.state,
            "duplicate_blocked": self.duplicate_blocked,
            "logs": list(self.logs),
            "tickets": {k: asdict(v) for k, v in self.tickets.items()},
        }


class Setup1OrderManagerV2:
    def __init__(self):
        self.active_plans: Dict[str, TwoLegPlan] = {}
        self.live_signatures: Dict[Tuple[str, str, str, float, int], str] = {}

    def _register_ticket(self, ticket: OrderTicket) -> None:
        self.live_signatures[ticket.signature()] = ticket.order_id

    def _unregister_ticket(self, ticket: OrderTicket) -> None:
        sig = ticket.signature()
        if sig in self.live_signatures:
            del self.live_signatures[sig]

    def create_two_leg_plan(self, event_slug: str, slot_name: str, up_price: float, down_price: float, qty_per_leg: int) -> Tuple[Optional[TwoLegPlan], List[str]]:
        if qty_per_leg <= 0:
            return None, ["qty_per_leg must be > 0"]
        plan = TwoLegPlan(event_slug=event_slug, slot_name=slot_name, up_price=up_price, down_price=down_price, qty_per_leg=qty_per_leg)
        duplicates = [t.signature() for t in plan.tickets.values() if t.signature() in self.live_signatures]
        if duplicates:
            plan.duplicate_blocked = True
            plan.state = PLAN_ABORTED
            plan.logs.append("duplicate order blocked")
            return None, [f"duplicate blocked for {len(duplicates)} order(s)"]
        self.active_plans[plan.plan_id] = plan
        for ticket in plan.tickets.values():
            self._register_ticket(ticket)
        plan.state = PLAN_WORKING
        return plan, [f"plan {plan.plan_id} created for {event_slug} with equal qty={qty_per_leg} on both legs"]

    def get_plan(self, plan_id: str) -> TwoLegPlan:
        return self.active_plans[plan_id]

    def apply_fill(self, plan_id: str, leg: str, qty: int, price: Optional[float] = None) -> List[str]:
        plan = self.active_plans[plan_id]
        ticket = plan.tickets[leg]
        events = ticket.apply_fill(qty, price)
        plan.logs.extend(events)
        if leg in ("up_entry", "down_entry"):
            events.extend(self._reconcile_entry_state(plan))
        elif leg in ("up_exit", "down_exit"):
            events.extend(self._reconcile_exit_state(plan))
        plan.logs.extend(events)
        return events

    def _reconcile_entry_state(self, plan: TwoLegPlan) -> List[str]:
        events: List[str] = []
        up = plan.tickets["up_entry"]
        down = plan.tickets["down_entry"]
        if up.status == STATUS_FILLED and down.status == STATUS_FILLED:
            if plan.state != PLAN_HEDGED:
                plan.state = PLAN_HEDGED
                events.append("both entry orders fully filled -> hedged")
            return events
        if up.filled_qty > 0 and down.filled_qty == 0:
            events.append("only up leg has exposure")
        elif down.filled_qty > 0 and up.filled_qty == 0:
            events.append("only down leg has exposure")
        elif up.filled_qty > 0 and down.filled_qty > 0 and up.filled_qty != down.filled_qty:
            events.append(f"unbalanced hedge | up={up.filled_qty} down={down.filled_qty}")
        elif up.filled_qty > 0 and down.filled_qty > 0 and up.filled_qty == down.filled_qty:
            events.append(f"balanced partial hedge | qty={up.filled_qty}")
        if up.status == STATUS_FILLED and down.status == STATUS_PARTIAL:
            events.append("up full + down partial -> keep working partial until deadline or abort")
        elif down.status == STATUS_FILLED and up.status == STATUS_PARTIAL:
            events.append("down full + up partial -> keep working partial until deadline or abort")
        elif up.status == STATUS_PARTIAL and down.filled_qty == 0:
            events.append("only up partial -> cancel remainder and flatten if deadline hits")
        elif down.status == STATUS_PARTIAL and up.filled_qty == 0:
            events.append("only down partial -> cancel remainder and flatten if deadline hits")
        return events

    def on_deadline(self, plan_id: str) -> List[str]:
        plan = self.active_plans[plan_id]
        events: List[str] = []
        up = plan.tickets["up_entry"]
        down = plan.tickets["down_entry"]
        events.extend(up.cancel_remaining("deadline"))
        events.extend(down.cancel_remaining("deadline"))
        if up.filled_qty == 0 and down.filled_qty == 0:
            plan.state = PLAN_ABORTED
            events.append("deadline with no fills -> aborted cleanly")
        elif up.filled_qty == down.filled_qty and up.filled_qty > 0:
            plan.state = PLAN_HEDGED
            events.append(f"deadline with balanced filled hedge qty={up.filled_qty} -> can post exit")
        else:
            plan.state = PLAN_FORCE_CLOSED
            events.append(f"deadline with imbalance up={up.filled_qty} down={down.filled_qty} -> flatten exposed side")
        plan.logs.extend(events)
        return events

    def post_exit_orders(self, plan_id: str, up_exit_price: float, down_exit_price: float) -> List[str]:
        plan = self.active_plans[plan_id]
        up_qty = plan.tickets["up_entry"].filled_qty
        down_qty = plan.tickets["down_entry"].filled_qty
        qty = min(up_qty, down_qty)
        events: List[str] = []
        if qty <= 0:
            return ["cannot post exit: no balanced qty"]
        if "up_exit" not in plan.tickets:
            plan.tickets["up_exit"] = OrderTicket(plan_id=plan.plan_id, event_slug=plan.event_slug, slot_name=plan.slot_name, leg="up_exit", outcome="up", side="sell", price=float(up_exit_price), requested_qty=qty)
            if plan.tickets["up_exit"].signature() in self.live_signatures:
                return ["duplicate blocked for up_exit"]
            self._register_ticket(plan.tickets["up_exit"])
        if "down_exit" not in plan.tickets:
            plan.tickets["down_exit"] = OrderTicket(plan_id=plan.plan_id, event_slug=plan.event_slug, slot_name=plan.slot_name, leg="down_exit", outcome="down", side="sell", price=float(down_exit_price), requested_qty=qty)
            if plan.tickets["down_exit"].signature() in self.live_signatures:
                return ["duplicate blocked for down_exit"]
            self._register_ticket(plan.tickets["down_exit"])
        plan.state = PLAN_EXIT_POSTED
        events.append(f"exit orders posted with equal qty={qty}")
        plan.logs.extend(events)
        return events

    def _reconcile_exit_state(self, plan: TwoLegPlan) -> List[str]:
        events: List[str] = []
        up_exit = plan.tickets.get("up_exit")
        down_exit = plan.tickets.get("down_exit")
        if not up_exit or not down_exit:
            return events
        if up_exit.status == STATUS_FILLED and down_exit.status == STATUS_FILLED:
            plan.state = PLAN_DONE
            events.append("both exit orders fully filled -> done")
        elif up_exit.filled_qty > 0 or down_exit.filled_qty > 0:
            events.append(f"partial exit progress | up_exit={up_exit.filled_qty}/{up_exit.requested_qty} down_exit={down_exit.filled_qty}/{down_exit.requested_qty}")
        return events

    def force_close_plan(self, plan_id: str, reason: str, up_close_price: Optional[float] = None, down_close_price: Optional[float] = None) -> List[str]:
        plan = self.active_plans[plan_id]
        events: List[str] = []
        for leg in ("up_entry", "down_entry", "up_exit", "down_exit"):
            ticket = plan.tickets.get(leg)
            if not ticket:
                continue
            if ticket.status not in (STATUS_FILLED, STATUS_CLOSED):
                events.extend(ticket.cancel_remaining(reason))
            if leg in ("up_entry", "down_entry") and ticket.filled_qty > 0:
                close_price = up_close_price if leg.startswith("up") else down_close_price
                events.extend(ticket.close_manually(reason, close_price))
        plan.state = PLAN_FORCE_CLOSED
        plan.logs.extend(events)
        return events

    def close_plan(self, plan_id: str) -> None:
        plan = self.active_plans[plan_id]
        for ticket in plan.tickets.values():
            self._unregister_ticket(ticket)
