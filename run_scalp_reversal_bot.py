from __future__ import annotations

import argparse
from pprint import pprint

from market.broker_env import load_broker_env
from market.live_guarded_config import load_live_guarded_config
from market.live_scalp_reversal_v1 import _load_scalp_cfg_v1
from market.live_scalp_reversal_v1 import monitor_live_scalp_reversal_v1


def _validate_preflight() -> tuple[bool, str]:
    broker_status = load_broker_env()
    guarded = load_live_guarded_config()
    scalp = _load_scalp_cfg_v1()

    print("[BROKER_ENV]")
    pprint(broker_status.as_dict())
    print("[LIVE_GUARDED_CONFIG]")
    pprint(guarded.as_dict())
    print("[SCALP_CONFIG]")
    pprint(vars(scalp))

    if not scalp.enabled:
        return False, "Scalp runner is disabled. Set POLY_SCALP_ENABLED=true."
    if not broker_status.ready_for_real_smoke:
        return False, "Broker env is not ready. Fill required POLY_* credentials in .env."
    if guarded.shadow_only:
        return False, "Scalp rollout requires POLY_GUARDED_SHADOW_ONLY=false."
    if not guarded.real_posts_enabled:
        return False, "Scalp rollout requires POLY_GUARDED_REAL_POSTS_ENABLED=true."
    return True, "Ready for live scalp reversal monitoring."


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m scalp reversal runner")
    parser.add_argument("--seconds", type=int, default=None, help="Optional run duration override")
    parser.add_argument("--preflight-only", action="store_true", help="Run only preflight checks and exit")
    args = parser.parse_args()

    print("[BOOT] Starting scalp reversal runner v1...")
    ok, msg = _validate_preflight()
    print(f"[RESULT] {msg}")
    if not ok:
        return 1

    if args.preflight_only:
        return 0

    monitor_live_scalp_reversal_v1(duration_seconds=args.seconds)
    print("[RUN] scalp reversal runner finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
