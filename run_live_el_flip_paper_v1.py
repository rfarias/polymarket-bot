"""Runner paper EL Flip — detecta inversão dominante (gap>=0.35) no BTC 5m."""
import argparse
import sys
from market.live_el_flip_paper_v1 import run_el_flip_paper_v1

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=3600)
    ap.add_argument("--poll",    type=float, default=1.5)
    ap.add_argument("--qty",     type=float, default=6.0)
    args = ap.parse_args()
    sys.exit(run_el_flip_paper_v1(
        run_seconds=args.seconds,
        poll_secs=args.poll,
        qty=args.qty,
    ) or 0)
