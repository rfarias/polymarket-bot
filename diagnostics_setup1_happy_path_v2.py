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
    snap = executor.snapshot()
    pprint(snap)
    return snap


def main():
    executor = Setup1DryRunExecutor()
    slot_name = "next_2"
    event_slug = "btc-updown-5m-happy-path-v2"

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
        "happy path v2 - create plan",
        executor,
        slot_name=slot_name,
        event_slug=event_slug,
        signal="armed",
        metrics=create_metrics,
        secs_to_end=700,
    )

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
        "happy path v2 - finish entry fills and post exit",
        executor,
        slot_name=slot_name,
        event_slug=event_slug,
        signal="armed",
        metrics=fill_metrics,
        secs_to_end=698,
    )

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

    # Keep sending favorable exit ticks until the plan disappears from the slot.
    for idx, secs in enumerate([696, 694, 692, 690], start=1):
        snap = run_case(
            f"happy path v2 - exit tick {idx}",
            executor,
            slot_name=slot_name,
            event_slug=event_slug,
            signal="armed",
            metrics=exit_metrics,
            secs_to_end=secs,
        )
        if snap["slots"][slot_name]["active_plan_id"] is None:
            print("\n[RESULT] Happy path completed and plan closed")
            break
    else:
        print("\n[RESULT] Happy path v2 ended without fully closing the plan")


if __name__ == "__main__":
    main()
