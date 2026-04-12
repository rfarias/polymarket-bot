from pprint import pprint

from market.broker_reconciliation_v1 import reconcile_executor_with_broker_open_orders
from market.broker_types import BrokerOrder
from market.dryrun_broker import DryRunBroker
from market.setup1_broker_executor_v3 import Setup1BrokerExecutorV3


def main():
    broker = DryRunBroker()
    executor = Setup1BrokerExecutorV3(broker=broker, shadow_only=False)

    plan_id = "plan-known-123"
    known_client_key = f"{plan_id}:up_entry"
    executor.plan_broker_orders[plan_id] = {
        "up_entry": {
            "mode": "shadow",
            "request": {
                "token_id": "btc-updown-5m-known:UP",
                "side": "BUY",
                "price": 0.5,
                "size": 5.0,
                "order_type": "GTC",
                "market_slug": "btc-updown-5m-known",
                "outcome": "UP",
                "client_order_key": known_client_key,
            },
            "order": {
                "order_id": "known-order-1",
                "token_id": "btc-updown-5m-known:UP",
                "side": "BUY",
                "price": 0.5,
                "original_size": 5.0,
                "size_matched": 0.0,
                "status": "open",
                "outcome": "UP",
                "market_slug": "btc-updown-5m-known",
                "order_type": "GTC",
                "raw": {"client_order_key": known_client_key},
                "remaining_size": 5.0,
            },
            "status": "shadow_posted",
        }
    }

    broker_open_orders = [
        BrokerOrder(
            order_id="known-order-1",
            token_id="btc-updown-5m-known:UP",
            side="BUY",
            price=0.5,
            original_size=5.0,
            size_matched=1.0,
            status="partial",
            outcome="UP",
            market_slug="btc-updown-5m-known",
            order_type="GTC",
            raw={"client_order_key": known_client_key},
        ),
        BrokerOrder(
            order_id="external-order-1",
            token_id="btc-updown-5m-external:DOWN",
            side="SELL",
            price=0.63,
            original_size=7.0,
            size_matched=0.0,
            status="open",
            outcome="DOWN",
            market_slug="btc-updown-5m-external",
            order_type="GTC",
            raw={},
        ),
        BrokerOrder(
            order_id="unknown-key-order-1",
            token_id="btc-updown-5m-unknown:UP",
            side="BUY",
            price=0.41,
            original_size=6.0,
            size_matched=0.0,
            status="open",
            outcome="UP",
            market_slug="btc-updown-5m-unknown",
            order_type="GTC",
            raw={"client_order_key": "unknown-plan-999:down_entry"},
        ),
    ]

    print("[TEST] Starting broker reconcile diagnostic v1...")
    reconcile = reconcile_executor_with_broker_open_orders(executor, broker_open_orders)
    pprint(reconcile)

    print("\n[PLAN_BROKER_ORDERS_AFTER_RECONCILE]")
    pprint(executor.plan_broker_orders)


if __name__ == "__main__":
    main()
