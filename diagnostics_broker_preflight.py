from pprint import pprint

from market.broker_env import load_broker_env


def main():
    print("[TEST] Starting broker environment preflight...")
    status = load_broker_env()
    pprint(status.as_dict())

    if status.ready_for_real_smoke:
        print("[RESULT] Environment looks ready for diagnostics_broker_smoke.py")
    else:
        print("[RESULT] Environment is NOT ready for real broker smoke yet")
        if status.missing_required:
            print(f"[MISSING] {', '.join(status.missing_required)}")

    if status.warnings:
        print("[WARNINGS]")
        for warning in status.warnings:
            print(f"- {warning}")


if __name__ == "__main__":
    main()
