from __future__ import annotations

import argparse

from market.live_scalp_reversal_v1 import monitor_live_scalp_reversal_v1


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m scalp reversal runner")
    parser.add_argument("--seconds", type=int, default=None, help="Optional run duration override")
    args = parser.parse_args()

    print("[BOOT] Starting scalp reversal runner v1...")
    monitor_live_scalp_reversal_v1(duration_seconds=args.seconds)
    print("[RUN] scalp reversal runner finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
