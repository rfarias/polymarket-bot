from pprint import pprint

from market.dryrun_broker import DryRunBroker
from market.setup1_broker_executor_v3 import Setup1BrokerExecutorV3


def _sync_payload_from_broker(executor, broker, plan_id: str, leg: str):
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


def _broker_fill_partial(broker, order_id: str, qty: float):
    order = broker.get_order(order_id)
    if not order:
        return False
    executable = min(float(qty), order.remaining_size)
    if executable <= 0:
        return False
    order.size_matched += executable
    order.status = "filled" if order.remaining_size <= 0 else "partial"
    return True


def main():
    broker = DryRunBroker()
    executor = Setup1BrokerExecutorV3(broker=broker, shadow_only=False)

    tradable_metrics = {
        "up_bid": 0.49,
        "up_ask": 0.50,
        "down_bid": 0.50,
        "down_ask": 0.49,
        "sum_asks": 0.99,
        "sum_bids": 1.01,
        "edge_asks": 0.01,
        "edge_bids": 0.01,
    }

    print("\n=== CREATE PLAN ===")
    logs = executor.process_market_tick(
        slot_name="next_1",
        event_slug="btc-updown-5m-deadline-diagnostic",
        signal="armed",
        metrics=tradable_metrics,
        secs_to_end=700,
        deadline_trigger=330,
    )
    for line in logs:
        print(line)

    plan_id = executor.slots["next_1"].active_plan_id
    print(f"[PLAN_ID] {plan_id}")

    up_payload = executor.plan_broker_orders[plan_id]["up_entry"]
    down_payload = executor.plan_broker_orders[plan_id]["down_entry"]
    up_order_id = up_payload["order"]["order_id"]
    down_order_id = down_payload["order"]["order_id"]

    print("\n=== APPLY ASYMMETRIC PARTIAL FILLS ===")
    _broker_fill_partial(broker, up_order_id, 3)
    _broker_fill_partial(broker, down_order_id, 2)
    _sync_payload_from_broker(executor, broker, plan_id, "up_entry")
    _sync_payload_from_broker(executor, broker, plan_id, "down_entry")

    for event in executor.order_manager.apply_fill(plan_id, "up_entry", qty=3, price=0.50):
        print(f"[PLAN] next_1: {event}")
    for event in executor.order_manager.apply_fill(plan_id, "down_entry", qty=2, price=0.49):
        print(f"[PLAN] next_1: {event}")

    print("\n[SNAPSHOT_BEFORE_DEADLINE]")
    pprint(executor.snapshot())
    print("[OPEN_ORDERS_BEFORE_DEADLINE]")
    pprint([o.as_dict() for o in broker.get_open_orders()])

    print("\n=== TRIGGER DEADLINE ===")
    logs = executor.on_deadline(slot_name="next_1", metrics=tradable_metrics)
    for line in logs:
        print(line)

    print("\n[SNAPSHOT_AFTER_DEADLINE]")
    pprint(executor.snapshot())
    print("[OPEN_ORDERS_AFTER_DEADLINE]")
    pprint([o.as_dict() for o in broker.get_open_orders()])


if __name__ == "__main__":
    main()
