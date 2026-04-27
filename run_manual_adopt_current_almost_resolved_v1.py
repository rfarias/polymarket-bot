from __future__ import annotations

import argparse

from market.manual_adopt_current_almost_resolved_v1 import monitor_manual_adopt_current_almost_resolved_v1


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual-adopt manager for current almost resolved")
    parser.add_argument("--seconds", type=int, default=None, help="Optional run duration override")
    parser.add_argument("--log-dir", type=str, default=None, help="Optional session log directory")
    args = parser.parse_args()

    monitor_manual_adopt_current_almost_resolved_v1(duration_seconds=args.seconds, log_dir=args.log_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
