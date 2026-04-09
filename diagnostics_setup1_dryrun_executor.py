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
        "blocked by exit gap",
        executor,
        slot_name="next_1",
        event_slug="btc-updown-5m-demo-blocked",
        signal="watching",
        metrics=blocked_metrics,
        secs_to_end=500,
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
        "plan created",
        executor,
        slot_name="next_2",
        event_slug="btc-updown-5m-demo-plan",
        signal="armed",
        metrics=good_metrics,
        secs_to_end=700,
        deadline_trigger=None,
    )

    run_case(
        "entry partial fills",
        executor,
        slot_name="next_2",
        event_slug="btc-updown-5m-demo-plan",
        signal="armed",
        metrics=good_metrics,
        secs_to_end=698,
        deadline_trigger=None,
    )

    better_metrics = {
        "up_bid": 0.52,
        "up_ask": 0.49,
        "down_bid": 0.51,
        "down_ask": 0.48,
        "sum_asks": 0.97,
        "sum_bids": 1.03,
        "edge_asks": 0.03,
        "edge_bids": 0.03,
    }
    run_case(
        "full fills and exit progress",
        executor,
        slot_name="next_2",
        event_slug="btc-updown-5m-demo-plan",
        signal="armed",
        metrics=better_metrics,
        secs_to_end=696,
        deadline_trigger=None,
    )

    deadline_metrics = {
        "up_bid": 0.46,
        "up_ask": 0.47,
        "down_bid": 0.53,
        "down_ask": 0.54,
        "sum_asks": 1.01,
        "sum_bids": 0.99,
        "edge_asks": -0.01,
        "edge_bids": -0.01,
    }
    run_case(
        "deadline force close",
        executor,
        slot_name="next_2",
        event_slug="btc-updown-5m-demo-plan",
        signal="watching",
        metrics=deadline_metrics,
        secs_to_end=300,
        deadline_trigger=330,
    )


if __name__ == "__main__":
    main()
