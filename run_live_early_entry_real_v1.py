"""
run_live_early_entry_real_v1.py

Entry point do runner real Early Entry (EE) v2.

Uso:
    python run_live_early_entry_real_v1.py [--seconds N]

Env vars (definidas pelo watchdog):
    EE_REAL_ENABLED            — "true" para habilitar o runner
    EE_REAL_POSTS_ENABLED      — "true" para postar ordens reais (requer -ArmReal no watchdog)
    EE_REAL_QTY                — qty por trade (default: 6)
    EE_REAL_POLL_SECS          — intervalo de poll em segundos (default: 0.5)
    EE_REAL_RUN_SECONDS        — duração da sessão (default: 3600)
"""
import argparse

from market.live_early_entry_real_v1 import run_early_entry_real_v1

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Early Entry real runner v2")
    parser.add_argument("--seconds", type=int, default=None, help="Duração da sessão em segundos")
    args = parser.parse_args()
    run_early_entry_real_v1(duration_seconds=args.seconds)
