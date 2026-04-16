from __future__ import annotations

from market.dryrun_broker import DryRunBroker
from market.real_execution_workflow_v2 import maybe_post_balanced_exit_orders_v2
from market.setup1_broker_executor_v4 import Setup1BrokerExecutorV4


def _fail(message: str) -> int:
    print(f"[FAIL] {message}")
    return 1


def main() -> int:
    broker = DryRunBroker()
    executor = Setup1BrokerExecutorV4(
        broker=broker,
        shadow_only=False,
        min_shares_per_leg=5,
    )

    metrics = {
        "up_bid": 0.49,
        "up_ask": 0.50,
        "down_bid": 0.50,
        "down_ask": 0.49,
        "sum_asks": 0.99,
        "sum_bids": 0.99,
        "edge_asks": 0.01,
        "edge_bids": -0.01,
    }

    logs = executor.evaluate_slot(
        slot_name="next_1",
        event_slug="btc-updown-5m-balanced-hold",
        signal="armed",
        metrics=metrics,
        secs_to_end=700,
        outcome_token_ids={"UP": "up-token", "DOWN": "down-token"},
    )
    for line in logs:
        print(line)

    plan_id = executor.slots["next_1"].active_plan_id
    if not plan_id:
        return _fail("expected active plan in next_1")

    for event in executor.order_manager.apply_fill(plan_id, "up_entry", qty=5, price=0.50):
        print(f"[PLAN] next_1: {event}")
    for event in executor.order_manager.apply_fill(plan_id, "down_entry", qty=5, price=0.49):
        print(f"[PLAN] next_1: {event}")

    hold_logs_1 = maybe_post_balanced_exit_orders_v2(executor, slot_name="next_1", metrics=metrics)
    hold_logs_2 = maybe_post_balanced_exit_orders_v2(executor, slot_name="next_1", metrics=metrics)
    for line in hold_logs_1:
        print(line)
    for line in hold_logs_2:
        print(line)

    plan = executor.order_manager.get_plan(plan_id)
    plan_payload = executor.plan_broker_orders.get(plan_id) or {}

    if "up_exit" in plan.tickets or "down_exit" in plan.tickets:
        return _fail("balanced hedge must not create exit tickets")
    if "up_exit" in plan_payload or "down_exit" in plan_payload:
        return _fail("balanced hedge must not post broker exit orders")
    if len(hold_logs_1) != 1:
        return _fail("expected one hold log on first balanced call")
    if hold_logs_2:
        return _fail("expected idempotent balanced hold log on subsequent calls")

    print("[PASS] balanced hedge is held until resolution without posting exits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
