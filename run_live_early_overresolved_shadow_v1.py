from __future__ import annotations

import argparse
from pprint import pprint

from market.live_early_overresolved_shadow_v1 import monitor_live_early_overresolved_shadow_v1


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket BTC current early overresolved shadow runner")
    parser.add_argument("--seconds", type=int, default=None, help="Optional run duration override")
    args = parser.parse_args()

    print("[BOOT] Starting early overresolved shadow runner v1...")
    print("[MODE]")
    pprint({"shadow_only": True, "posts_real_orders": False})
    monitor_live_early_overresolved_shadow_v1(duration_seconds=args.seconds)
    print("[RUN] early overresolved shadow runner finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
