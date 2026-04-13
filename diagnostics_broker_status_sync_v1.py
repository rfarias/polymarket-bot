from pprint import pprint

from market.broker_status_sync_v2 import sync_executor_from_broker_open_orders_v2
from market.broker_types import BrokerOrder
from market.dryrun_broker import DryRunBroker
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

    print("[TEST] Starting broker status sync diagnostic v1...")
    logs = executor.process_market_tick(
        slot_name="next_1",
        event_slug="btc-updown-5m-sync-diagnostic",
        signal="armed",
        metrics=tradable_metrics,
        secs_to_end=700,
        deadline_trigger=330,
    )
    for line in logs:
        print(line)

    plan_id = executor.slots["next_1"].active_plan_id
    up_payload = executor.plan_broker_orders[plan_id]["up_entry"]
    down_payload = executor.plan_broker_orders[plan_id]["down_entry"]

    # Convert shadow payloads into broker-like tracked orders with order ids.
    up_payload["order"] = {
        "order_id": "sync-up-order-1",
        "token_id": up_payload["request"]["token_id"],
        "side": "BUY",
        "price": up_payload["request"]["price"],
        "original_size": up_payload["request"]["size"],
        "size_matched": 0.0,
        "status": "open",
        "outcome": "UP",
        "market_slug": up_payload["request"]["market_slug"],
        "order_type": "GTC",
        "raw": {"client_order_key": up_payload["request"]["client_order_key"]},
    }
    down_payload["order"] = {
        "order_id": "sync-down-order-1",
        "token_id": down_payload["request"]["token_id"],
        "side": "BUY",
        "price": down_payload["request"]["price"],
        "original_size": down_payload["request"]["size"],
        "size_matched": 0.0,
        "status": "open",
        "outcome": "DOWN",
        "market_slug": down_payload["request"]["market_slug"],
        "order_type": "GTC",
        "raw": {"client_order_key": down_payload["request"]["client_order_key"]},
    }

    broker_open_orders = [
        BrokerOrder(
            order_id="sync-up-order-1",
            token_id=up_payload["request"]["token_id"],
            side="BUY",
            price=0.50,
            original_size=5.0,
            size_matched=2.0,
            status="partial",
            outcome="UP",
            market_slug=up_payload["request"]["market_slug"],
            order_type="GTC",
            raw={"client_order_key": up_payload["request"]["client_order_key"]},
        ),
        BrokerOrder(
            order_id="sync-down-order-1",
            token_id=down_payload["request"]["token_id"],
            side="BUY",
            price=0.49,
            original_size=5.0,
            size_matched=5.0,
            status="filled",
            outcome="DOWN",
            market_slug=down_payload["request"]["market_slug"],
            order_type="GTC",
            raw={"client_order_key": down_payload["request"]["client_order_key"]},
        ),
    ]

    print("\n=== APPLY BROKER STATUS SYNC (partial/full) ===")
    sync_logs, reconcile = sync_executor_from_broker_open_orders_v2(executor, broker_open_orders)
    for line in sync_logs:
        print(line)
    print("[RECONCILE]")
    pprint(reconcile)
    print("[SNAPSHOT_AFTER_SYNC_1]")
    pprint(executor.snapshot())

    broker_open_orders_2 = [
        BrokerOrder(
            order_id="sync-up-order-1",
            token_id=up_payload["request"]["token_id"],
            side="BUY",
            price=0.50,
            original_size=5.0,
            size_matched=2.0,
            status="canceled",
            outcome="UP",
            market_slug=up_payload["request"]["market_slug"],
            order_type="GTC",
            raw={"client_order_key": up_payload["request"]["client_order_key"]},
        ),
        BrokerOrder(
            order_id="sync-down-order-1",
            token_id=down_payload["request"]["token_id"],
            side="BUY",
            price=0.49,
            original_size=5.0,
            size_matched=5.0,
            status="filled",
            outcome="DOWN",
            market_slug=down_payload["request"]["market_slug"],
            order_type="GTC",
            raw={"client_order_key": down_payload["request"]["client_order_key"]},
        ),
    ]

    print("\n=== APPLY BROKER STATUS SYNC (cancel remaining) ===")
    sync_logs_2, reconcile_2 = sync_executor_from_broker_open_orders_v2(executor, broker_open_orders_2)
    for line in sync_logs_2:
        print(line)
    print("[RECONCILE_2]")
    pprint(reconcile_2)
    print("[SNAPSHOT_AFTER_SYNC_2]")
    pprint(executor.snapshot())


if __name__ == "__main__":
    main()
