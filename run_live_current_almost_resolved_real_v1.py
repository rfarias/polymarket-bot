from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from pprint import pprint

from market.broker_env import load_broker_env
from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1
from market.current_scalp_signal_v1 import CurrentScalpConfigV1
from market.live_current_almost_resolved_real_v1 import monitor_live_current_almost_resolved_real_v1
from market.live_guarded_config import load_live_guarded_config
from market.polymarket_broker_v3 import PolymarketBrokerV3


def _state_order_ids() -> set[str]:
    state_path = Path("logs") / "current_almost_resolved_real_state.json"
    if not state_path.exists():
        return set()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {str(payload.get(key)) for key in ("entry_order_id", "exit_order_id") if payload.get(key)}


def _validate_preflight() -> tuple[bool, str]:
    broker_status = load_broker_env()
    guarded = load_live_guarded_config()
    signal_cfg = CurrentAlmostResolvedConfigV1()
    scalp_cfg = CurrentScalpConfigV1()

    print("[BROKER_ENV]")
    pprint(broker_status.as_dict())
    print("[LIVE_GUARDED_CONFIG]")
    pprint(guarded.as_dict())
    print("[CURRENT_ALMOST_RESOLVED_CONFIG]")
    pprint(signal_cfg.as_dict())
    print("[CURRENT_SCALP_CONTEXT_CONFIG]")
    pprint(scalp_cfg.as_dict())

    if not guarded.enabled:
        return False, "Current almost resolved real requires POLY_GUARDED_ENABLED=true."
    if guarded.shadow_only:
        return False, "Current almost resolved real requires POLY_GUARDED_SHADOW_ONLY=false."
    if not guarded.real_posts_enabled:
        return False, "Current almost resolved real requires POLY_GUARDED_REAL_POSTS_ENABLED=true."
    if str(os.getenv("POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED", "false")).strip().lower() != "true":
        return False, "Current almost resolved real requires POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED=true."
    if not broker_status.ready_for_real_smoke:
        return False, "Broker env is not ready. Fill required POLY_* credentials in .env."

    qty = int(os.getenv("POLY_CURRENT_ALMOST_RESOLVED_QTY", "5"))
    if qty < 5:
        return False, "Current almost resolved real requires POLY_CURRENT_ALMOST_RESOLVED_QTY >= 5."

    broker = PolymarketBrokerV3.from_env()
    health = broker.healthcheck()
    print("[BROKER_HEALTH]")
    pprint(health.as_dict())
    if not health.ok:
        return False, f"Broker healthcheck failed: {health.message}"

    startup_orders = broker.get_open_orders()[:50]
    print("[BROKER_OPEN_ORDERS_STARTUP]")
    pprint([order.as_dict() for order in startup_orders])
    if startup_orders:
        startup_ids = {order.order_id for order in startup_orders}
        allowed_ids = _state_order_ids()
        if not allowed_ids or startup_ids - allowed_ids:
            return False, "Startup guard blocked execution. Clear existing open orders or stop other real runners first."
        print("[STARTUP_GUARD] Existing open orders match restored current almost resolved state.")

    return True, "Ready for live current almost resolved monitoring."


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m current almost-resolved real runner")
    parser.add_argument("--seconds", type=int, default=None, help="Optional run duration override")
    parser.add_argument("--preflight-only", action="store_true", help="Run only preflight checks and exit")
    args = parser.parse_args()

    print("[BOOT] Starting current almost resolved real runner v1...")
    ok, msg = _validate_preflight()
    print(f"[RESULT] {msg}")
    if not ok:
        return 1

    if args.preflight_only:
        return 0

    monitor_live_current_almost_resolved_real_v1(duration_seconds=args.seconds)
    print("[RUN] current almost resolved real runner finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
