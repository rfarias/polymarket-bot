from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from market.polymarket_broker_v3 import PolymarketBrokerV3


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only broker health/open-orders monitor")
    parser.add_argument("--seconds", type=int, default=10800)
    parser.add_argument("--poll-secs", type=float, default=60.0)
    parser.add_argument("--log-file", type=str, default="logs/broker_open_orders_monitor.jsonl")
    args = parser.parse_args()

    broker = PolymarketBrokerV3.from_env()
    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    started_at = time.time()
    while time.time() - started_at < args.seconds:
        now = time.time()
        try:
            row = {
                "ts": now,
                "health": broker.healthcheck().as_dict(),
            }
            orders = [o.as_dict() for o in broker.get_open_orders()[:20]]
            row["open_orders_count"] = len(orders)
            row["open_orders"] = orders
        except Exception as exc:
            row = {
                "ts": now,
                "error": f"{type(exc).__name__}: {exc}",
            }

        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(row)
        time.sleep(max(5.0, float(args.poll_secs)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
