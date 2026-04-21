from __future__ import annotations

import argparse
import os
from pprint import pprint

from market.broker_env import load_broker_env
from market.live_current_scalp_real_v1 import monitor_live_current_scalp_real_v1
from market.live_guarded_config import load_live_guarded_config
from market.polymarket_broker_v3 import PolymarketBrokerV3
from market.current_scalp_signal_v1 import CurrentScalpConfigV1


def _validate_preflight() -> tuple[bool, str]:
    broker_status = load_broker_env()
    guarded = load_live_guarded_config()
    scalp = CurrentScalpConfigV1()

    print("[BROKER_ENV]")
    pprint(broker_status.as_dict())
    print("[LIVE_GUARDED_CONFIG]")
    pprint(guarded.as_dict())
    print("[CURRENT_SCALP_CONFIG]")
    pprint(scalp.as_dict())

    if not guarded.enabled:
        return False, "Current scalp real requires POLY_GUARDED_ENABLED=true."
    if guarded.shadow_only:
        return False, "Current scalp real requires POLY_GUARDED_SHADOW_ONLY=false."
    if not guarded.real_posts_enabled:
        return False, "Current scalp real requires POLY_GUARDED_REAL_POSTS_ENABLED=true."
    if str(os.getenv("POLY_CURRENT_SCALP_REAL_ENABLED", "false")).strip().lower() != "true":
        return False, "Current scalp real requires POLY_CURRENT_SCALP_REAL_ENABLED=true."
    if not broker_status.ready_for_real_smoke:
        return False, "Broker env is not ready. Fill required POLY_* credentials in .env."

    broker = PolymarketBrokerV3.from_env()
    health = broker.healthcheck()
    print("[BROKER_HEALTH]")
    pprint(health.as_dict())
    if not health.ok:
        return False, f"Broker healthcheck failed: {health.message}"

    startup_orders = broker.get_open_orders()[:50]
    print("[BROKER_OPEN_ORDERS_STARTUP]")
    pprint([order.as_dict() for order in startup_orders])
    return True, "Ready for live current scalp monitoring."


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m current scalp real runner")
    parser.add_argument("--seconds", type=int, default=None, help="Optional run duration override")
    parser.add_argument("--preflight-only", action="store_true", help="Run only preflight checks and exit")
    args = parser.parse_args()

    print("[BOOT] Starting current scalp real runner v1...")
    ok, msg = _validate_preflight()
    print(f"[RESULT] {msg}")
    if not ok:
        return 1

    if args.preflight_only:
        return 0

    monitor_live_current_scalp_real_v1(duration_seconds=args.seconds)
    print("[RUN] current scalp real runner finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
