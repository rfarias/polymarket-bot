"""
run_live_early_entry_paper_v1.py

Entry point do runner paper Early Entry (EE).
Roda em paralelo com o runner AR real sem postar ordens.

Uso:
    python run_live_early_entry_paper_v1.py [--seconds N]

Env vars opcionais:
    EE_PAPER_QTY           — qty simulada (default: 6)
    EE_PAPER_POLL_SECS     — intervalo de poll em segundos (default: 0.5)
    EE_PAPER_RUN_SECONDS   — duração total da sessão (default: 3600)
"""
import argparse

from market.live_early_entry_paper_v1 import run_early_entry_paper_v1

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Early Entry paper runner")
    parser.add_argument("--seconds", type=int, default=None, help="Duração da sessão em segundos")
    args = parser.parse_args()
    run_early_entry_paper_v1(duration_seconds=args.seconds)
