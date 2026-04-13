from pprint import pprint

from market.hybrid_focus_policy_v1 import summarize_focus_v1


def main():
    scenarios = [
        {
            "name": "next1_early_no_active_plan",
            "kwargs": {
                "next_1_secs_to_end": 820,
                "next_1_active_plan_id": None,
                "max_active_plans_reached": False,
                "allow_next_2_config": True,
            },
        },
        {
            "name": "next1_early_after_single_leg_profit",
            "kwargs": {
                "next_1_secs_to_end": 760,
                "next_1_active_plan_id": None,
                "max_active_plans_reached": False,
                "allow_next_2_config": True,
            },
        },
        {
            "name": "next1_active_plan_blocks_next2",
            "kwargs": {
                "next_1_secs_to_end": 430,
                "next_1_active_plan_id": "plan-123",
                "max_active_plans_reached": False,
                "allow_next_2_config": True,
            },
        },
        {
            "name": "next1_close_enough_enable_next2",
            "kwargs": {
                "next_1_secs_to_end": 420,
                "next_1_active_plan_id": None,
                "max_active_plans_reached": False,
                "allow_next_2_config": True,
            },
        },
        {
            "name": "guard_disabled_next2",
            "kwargs": {
                "next_1_secs_to_end": 420,
                "next_1_active_plan_id": None,
                "max_active_plans_reached": False,
                "allow_next_2_config": False,
            },
        },
    ]

    print("[TEST] Starting hybrid focus policy diagnostic v1...")
    for scenario in scenarios:
        print(f"\n=== SCENARIO {scenario['name']} ===")
        pprint(summarize_focus_v1(**scenario["kwargs"]))


if __name__ == "__main__":
    main()
