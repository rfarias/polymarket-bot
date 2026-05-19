"""
run_live_last_second_snipe_v1.py

Entry point for the Last-Second Snipe strategy.

Usage:
  python run_live_last_second_snipe_v1.py --mode safe --dry-run
  python run_live_last_second_snipe_v1.py --mode safe
  python run_live_last_second_snipe_v1.py --mode degen --windows 10
  python run_live_last_second_snipe_v1.py --mode aggressive --bankroll 50

.env variables used:
  POLY_PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE,
  POLY_FUNDER, POLY_SIGNATURE_TYPE, POLY_HOST, POLY_CHAIN_ID
  STARTING_BANKROLL (default: 10.0)
  MIN_BET (default: 1.0)
  BOT_MODE (default: safe)
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Last-Second Snipe — BTC 5min Polymarket")
    parser.add_argument("--mode", choices=["safe", "aggressive", "degen"],
                        default=os.getenv("BOT_MODE", "safe"), help="Trading mode")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate without real orders")
    parser.add_argument("--bankroll", type=float,
                        default=float(os.getenv("STARTING_BANKROLL", "10.0")),
                        help="Starting bankroll in USDC")
    parser.add_argument("--min-bet", type=float,
                        default=float(os.getenv("MIN_BET", "1.0")),
                        help="Minimum bet size in USDC")
    parser.add_argument("--windows", type=int, default=0,
                        help="Max windows to trade (0 = run forever)")
    parser.add_argument("--log-dir", default="logs", help="Log directory")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    from market.live_last_second_snipe_v1 import SnipeMode, run_last_second_snipe
    from market.broker_env import load_broker_env

    mode = SnipeMode.from_name(args.mode)
    dry_run = args.dry_run

    broker = None
    if not dry_run:
        env = load_broker_env()
        if env.missing_required:
            print(f"[ERROR] Missing env vars: {env.missing_required}")
            sys.exit(1)
        from market.polymarket_broker_v3 import PolymarketBrokerV3
        broker = PolymarketBrokerV3.from_env()
        try:
            broker.healthcheck()
            print("[OK] Broker connected")
        except Exception as exc:
            print(f"[ERROR] Broker healthcheck failed: {exc}")
            sys.exit(1)
    else:
        print("[INFO] Dry-run mode — no real orders will be placed")

    run_last_second_snipe(
        broker=broker,
        mode=mode,
        starting_bankroll=args.bankroll,
        dry_run=dry_run,
        min_bet=args.min_bet,
        max_windows=args.windows,
        log_dir=args.log_dir,
    )


if __name__ == "__main__":
    main()
