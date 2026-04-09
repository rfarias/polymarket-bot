from pprint import pprint

from market.dryrun_broker import DryRunBroker
from market.setup1_broker_executor_v3 import Setup1BrokerExecutorV3


def run_case(title, executor, *, slot_name, event_slug, signal, metrics, secs_to_end, deadline_trigger=None):
    print(f"\n=== {title} ===")
    logs = executor.process_market_tick(
        slot_name=slot_name,
        event_slug=event_slug,
        signal=signal,
        metrics=metrics,
        secs_to_end=secs_to_end,
        deadline_trigger=deadline_trigger,
    )
    for line in logs:
        print(line)
    snap = executor.snapshot()
    pprint(snap)
    print("[OPEN_ORDERS]")
    pprint([o.as_dict() for o in executor.broker.get_open_orders()])
    return snap


def main():
    broker = DryRunBroker()
    executor = Setup1BrokerExecutorV3(broker=broker, shadow_only=False)

    good_metrics = {
        "up_bid": 0.50,
        "up_ask": 0.50,
        "down_bid": 0.50,
        "down_ask": 0.49,
        "sum_asks": 0.99,
        "sum_bids": 1.00,
        "edge_asks": 0.01,
        "edge_bids": 0.00,
    }

    run_case(
        "v3 broker executor plan created with broker orders",
        executor,
        slot_name="next_2",
        event_slug="btc-updown-5m-broker-good-v3",
        signal="armed",
        metrics=good_metrics,
        secs_to_end=700,
    )

    snap = run_case(
        "v3 broker executor deadline cancel without fills",
        executor,
        slot_name="next_2",
        event_slug="btc-updown-5m-broker-good-v3",
        signal="watching",
        metrics=good_metrics,
        secs_to_end=320,
        deadline_trigger=330,
    )

    plan_orders = next(iter(snap["plan_broker_orders"].values()))
    up_status = plan_orders["up_entry"]["order"]["status"]
    down_status = plan_orders["down_entry"]["order"]["status"]
    if up_status == "canceled" and down_status == "canceled":
        print("[RESULT] Snapshot broker order statuses reconciled to canceled")
    else:
        print(f"[RESULT] Unexpected broker snapshot statuses: up={up_status} down={down_status}")


if __name__ == "__main__":
    main()
