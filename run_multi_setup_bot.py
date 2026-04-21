from __future__ import annotations

import argparse
from pprint import pprint

from market.broker_env import load_broker_env
from market.live_guarded_config import load_live_guarded_config
from market.live_multi_setup_v1 import monitor_live_multi_setup_v1
import os


def _validate_preflight() -> tuple[bool, str]:
    broker_status = load_broker_env()
    cfg = load_live_guarded_config()

    print("[BROKER_ENV]")
    pprint(broker_status.as_dict())
    print("[LIVE_GUARDED_CONFIG]")
    pprint(cfg.as_dict())
    print("[CURRENT_RUNTIME_FLAGS]")
    pprint(
        {
            "POLY_CURRENT_SCALP_SHADOW_ONLY": os.getenv("POLY_CURRENT_SCALP_SHADOW_ONLY", "true"),
            "POLY_CURRENT_ALMOST_RESOLVED_SHADOW_ONLY": os.getenv("POLY_CURRENT_ALMOST_RESOLVED_SHADOW_ONLY", "true"),
        }
    )

    if not cfg.enabled:
        return False, "Runner is disabled. Set POLY_GUARDED_ENABLED=true."
    if not broker_status.ready_for_real_smoke:
        return False, "Broker env is not ready. Fill required POLY_* credentials in .env."
    if cfg.shadow_only:
        return False, "This rollout requires POLY_GUARDED_SHADOW_ONLY=false."
    if not cfg.real_posts_enabled:
        return False, "This rollout requires POLY_GUARDED_REAL_POSTS_ENABLED=true."
    if cfg.allow_next_2:
        return False, "This rollout requires POLY_GUARDED_ALLOW_NEXT_2=false."
    if cfg.max_active_plans != 1:
        return False, "This rollout requires POLY_GUARDED_MAX_ACTIVE_PLANS=1."
    if cfg.min_shares_per_leg != 5:
        return False, "This rollout requires POLY_GUARDED_MIN_SHARES=5."
    if cfg.require_signal != "armed":
        return False, "This rollout requires POLY_GUARDED_REQUIRE_SIGNAL=armed."
    return True, "Ready for multi-setup monitoring: next1 arb + current scalp + almost-resolved fallback."


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket multi-setup launcher")
    parser.add_argument("--seconds", type=int, default=None, help="Optional override for run duration in seconds")
    parser.add_argument("--preflight-only", action="store_true", help="Run only preflight checks and exit")
    args = parser.parse_args()

    print("[BOOT] Starting multi-setup bot launcher...")
    ok, msg = _validate_preflight()
    print(f"[RESULT] {msg}")
    if not ok:
        return 1

    if args.preflight_only:
        return 0

    print("[RUN] Starting monitor_live_multi_setup_v1...")
    monitor_live_multi_setup_v1(duration_seconds=args.seconds)
    print("[RUN] monitor_live_multi_setup_v1 finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
