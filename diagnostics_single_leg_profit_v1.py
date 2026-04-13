from pprint import pprint

from market.dryrun_broker import DryRunBroker
from market.hybrid_single_leg_profit_v1 import maybe_take_single_leg_profit_v1
from market.setup1_broker_executor_v3 import Setup1BrokerExecutorV3


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

    print("[TEST] Starting single-leg profit diagnostic v1...")
    logs = executor.process_market_tick(
        slot_name="next_1",
        event_slug="btc-updown-5m-single-leg-profit",
        signal="armed",
        metrics=tradable_metrics,
        secs_to_end=700,
        deadline_trigger=330,
    )
    for line in logs:
        print(line)

    plan_id = executor.slots["next_1"].active_plan_id
    print(f"[PLAN_ID] {plan_id}")

    # Simulate only the UP leg fully filled.
    for event in executor.order_manager.apply_fill(plan_id, "up_entry", qty=5, price=0.48):
        print(f"[PLAN] next_1: {event}")

    # Adjust the tracked broker order snapshot to reflect the manual fill.
    up_payload = executor.plan_broker_orders[plan_id]["up_entry"]
    up_payload["order"]["price"] = 0.48
    up_payload["order"]["size_matched"] = 5.0
    up_payload["order"]["remaining_size"] = 0.0
    up_payload["order"]["status"] = "filled"

    print("\n[SNAPSHOT_BEFORE_TP]")
    pprint(executor.snapshot())

    profit_metrics = {
        "up_bid": 0.49,
        "up_ask": 0.50,
        "down_bid": 0.48,
        "down_ask": 0.49,
        "sum_asks": 0.99,
        "sum_bids": 0.97,
        "edge_asks": 0.01,
        "edge_bids": -0.03,
    }

    print("\n=== TRIGGER SINGLE-LEG PROFIT ===")
    tp_logs = maybe_take_single_leg_profit_v1(
        executor,
        slot_name="next_1",
        metrics=profit_metrics,
    )
    for line in tp_logs:
        print(line)

    print("\n[SNAPSHOT_AFTER_TP]")
    pprint(executor.snapshot())


if __name__ == "__main__":
    main()
