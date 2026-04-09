from pprint import pprint

from market.setup1_order_manager import Setup1OrderManager


def show(title, events, plan=None):
    print(f"\n=== {title} ===")
    for e in events:
        print(f"- {e}")
    if plan:
        pprint(plan.as_dict())


def main():
    manager = Setup1OrderManager()

    # Scenario 1: equal qty on both legs + duplicate prevention
    plan1, events1 = manager.create_two_leg_plan(
        event_slug="btc-updown-5m-demo-1",
        slot_name="next_1",
        up_price=0.48,
        down_price=0.53,
        qty_per_leg=5,
    )
    show("scenario1 create equal qty", events1, plan1)

    plan_dup, dup_events = manager.create_two_leg_plan(
        event_slug="btc-updown-5m-demo-1",
        slot_name="next_1",
        up_price=0.48,
        down_price=0.53,
        qty_per_leg=5,
    )
    show("scenario1 duplicate prevention", dup_events, plan_dup)

    # Scenario 2: one full, other partial
    plan2, events2 = manager.create_two_leg_plan(
        event_slug="btc-updown-5m-demo-2",
        slot_name="next_1",
        up_price=0.48,
        down_price=0.53,
        qty_per_leg=5,
    )
    show("scenario2 create", events2, plan2)
    if plan2:
        events = []
        events += manager.apply_fill(plan2.plan_id, "up_entry", qty=5, price=0.48)
        events += manager.apply_fill(plan2.plan_id, "down_entry", qty=2, price=0.53)
        show("scenario2 full up + partial down", events, plan2)
        deadline_events = manager.on_deadline(plan2.plan_id)
        show("scenario2 deadline handling", deadline_events, plan2)

    # Scenario 3: only one side partial
    plan3, events3 = manager.create_two_leg_plan(
        event_slug="btc-updown-5m-demo-3",
        slot_name="next_1",
        up_price=0.50,
        down_price=0.51,
        qty_per_leg=5,
    )
    show("scenario3 create", events3, plan3)
    if plan3:
        events = manager.apply_fill(plan3.plan_id, "up_entry", qty=2, price=0.50)
        show("scenario3 only one partial", events, plan3)
        deadline_events = manager.on_deadline(plan3.plan_id)
        show("scenario3 deadline handling", deadline_events, plan3)

    # Scenario 4: balanced partial hedge -> post exit with equal qty
    plan4, events4 = manager.create_two_leg_plan(
        event_slug="btc-updown-5m-demo-4",
        slot_name="next_2",
        up_price=0.49,
        down_price=0.50,
        qty_per_leg=5,
    )
    show("scenario4 create", events4, plan4)
    if plan4:
        events = []
        events += manager.apply_fill(plan4.plan_id, "up_entry", qty=3, price=0.49)
        events += manager.apply_fill(plan4.plan_id, "down_entry", qty=3, price=0.50)
        show("scenario4 balanced partial fills", events, plan4)
        deadline_events = manager.on_deadline(plan4.plan_id)
        show("scenario4 deadline handling", deadline_events, plan4)
        exit_events = manager.post_exit_orders(plan4.plan_id, up_exit_price=0.50, down_exit_price=0.51)
        show("scenario4 exit posting with equal balanced qty", exit_events, plan4)


if __name__ == "__main__":
    main()
