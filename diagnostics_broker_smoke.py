from pprint import pprint

from market.broker_factory import build_broker


def main():
    broker = build_broker(dry_run=False)

    print("[TEST] Starting broker smoke test (read-only authenticated)...")
    health = broker.healthcheck()
    print("[HEALTH]")
    pprint(health.as_dict())

    if not health.ok:
        print("[RESULT] Broker healthcheck failed; stop here and fix env/deps first.")
        return

    try:
        open_orders = broker.get_open_orders()
        print(f"[OPEN_ORDERS] count={len(open_orders)}")
        for order in open_orders[:10]:
            pprint(order.as_dict())
    except Exception as exc:
        print(f"[ERROR] Failed to fetch open orders: {type(exc).__name__}: {exc}")
        raise

    print("[RESULT] Broker smoke test finished successfully")


if __name__ == "__main__":
    main()
