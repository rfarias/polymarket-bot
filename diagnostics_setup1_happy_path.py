from pprint import pprint

from market.setup1_dryrun_executor import Setup1DryRunExecutor


def run_case(title, executor, slot_name, event_slug, signal, metrics, secs_to_end, deadline_trigger=None):
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


def main():
    executor = Setup1DryRunExecutor()

    # 1) Create plan with good quality
    create_metrics = {
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
        "happy path - create plan",
        executor,
        slot_name="next_2",
        event_slug="btc-updown-5m-happy-path",
        signal="armed",
        metrics=create_metrics,
        secs_to_end=700,
        deadline_trigger=None,
    )

    # 2) Better market fills remaining entries and posts exit
    fill_metrics = {
        "up_bid": 0.51,
        "up_ask": 0.48,
        "down_bid": 0.50,
        "down_ask": 0.47,
        "sum_asks": 0.95,
        "sum_bids": 1.01,
        "edge_asks": 0.05,
        "edge_bids": 0.01,
    }
    run_case(
        "happy path - finish entry fills and post exit",
        executor,
        slot_name="next_2",
        event_slug="btc-updown-5m-happy-path",
        signal="armed",
        metrics=fill_metrics,
        secs_to_end=698,
        deadline_trigger=None,
    )

    # 3) Even stronger bids should fill exits and end the plan
    exit_metrics = {
        "up_bid": 0.53,
        "up_ask": 0.49,
        "down_bid": 0.52,
        "down_ask": 0.48,
        "sum_asks": 0.97,
        "sum_bids": 1.05,
        "edge_asks": 0.03,
        "edge_bids": 0.05,
    }
    run_case(
        "happy path - fill exits and finish",
        executor,
        slot_name="next_2",
        event_slug="btc-updown-5m-happy-path",
        signal="armed",
        metrics=exit_metrics,
        secs_to_end=696,
        deadline_trigger=None,
    )


if __name__ == "__main__":
    main()
