"""Inicia o Spot Scalp paper runner."""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from market.live_spot_scalp_paper_v1 import run_spot_scalp_paper

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=float, default=float("inf"))
parser.add_argument("--poll",    type=float, default=1.0)
args = parser.parse_args()
run_spot_scalp_paper(run_for=args.seconds, poll_secs=args.poll)
