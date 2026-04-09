from pprint import pprint

from market.dryrun_broker import DryRunBroker
from market.setup1_broker_executor import Setup1BrokerExecutor


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
    pprint(executor.snapshot())
    print("[OPEN_ORDERS]")
    pprint([o.as_dict() for o in executor.broker.get_open_orders()])


def main():
    broker = DryRunBroker()
    executor = Setup1BrokerExecutor(broker=broker, shadow_only=False)

    blocked_metrics = {
        "up_bid": 0.50,
        "up_ask": 0.51,
        "down_bid": 0.49,
        "down_ask": 0.50,
        "sum_asks": 1.01,
        "sum_bids": 0.99,
        "edge_asks": -0.01,
        "edge_bids": -0.01,
    }
    run_case(
        "broker executor blocked",
        executor,
        slot_name="next_1",
        event_slug="btc-updown-5m-broker-blocked",
        signal="watching",
        metrics=blocked_metrics,
        secs_to_end=480,
        deadline_trigger=330,
    )

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
        "broker executor plan created with broker orders",
        executor,
        slot_name="next_2",
        event_slug="btc-updown-5m-broker-good",
        signal="armed",
        metrics=good_metrics,
        secs_to_end=700,
        deadline_trigger=None,
    )

    run_case(
        "broker executor deadline cancel",
        executor,
        slot_name="next_2",
        event_slug="btc-updown-5m-broker-good",
        signal="watching",
        metrics=good_metrics,
        secs_to_end=320,
        deadline_trigger=330,
    )


if __name__ == "__main__":
    main()
