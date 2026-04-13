from market.broker_startup_guard_v1 import evaluate_startup_guard
from market.live_guarded_config import load_live_guarded_config
from market.polymarket_broker_v2 import PolymarketBrokerV2
from market.setup1_broker_executor_v3 import Setup1BrokerExecutorV3
from market.live_minimal_guarded_v2 import monitor_live_minimal_guarded_v2


def monitor_live_minimal_guarded_v3(duration_seconds: int | None = None) -> None:
    cfg = load_live_guarded_config()
    print("[LIVE_GUARDED_CONFIG]", cfg.as_dict())

    if not cfg.enabled:
        print("[GUARD] Disabled. Set POLY_GUARDED_ENABLED=true to arm this runner.")
        return

    broker = PolymarketBrokerV2.from_env()
    executor = Setup1BrokerExecutorV3(
        broker=broker,
        shadow_only=True,
        min_shares_per_leg=cfg.min_shares_per_leg,
    )

    health = broker.healthcheck()
    print("[BROKER_HEALTH]", health.as_dict())
    if not health.ok:
        print("[GUARD] Broker healthcheck failed; aborting guarded runner.")
        return

    try:
        startup_orders = broker.get_open_orders()[:50]
        print("[BROKER_OPEN_ORDERS_STARTUP]")
        print([o.as_dict() for o in startup_orders])
        allowed, startup_report = evaluate_startup_guard(executor, startup_orders)
        print("[STARTUP_GUARD]")
        print(startup_report)
        if not allowed:
            print("[GUARD] Startup blocked because external or unknown open orders exist in the broker account.")
            return
    except Exception as exc:
        print(f"[STARTUP_GUARD_ERROR] {type(exc).__name__}: {exc}")
        return

    monitor_live_minimal_guarded_v2(duration_seconds=duration_seconds)