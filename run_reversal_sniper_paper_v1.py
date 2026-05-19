"""
run_reversal_sniper_paper_v1.py

Entrypoint para o Reversal Sniper — Fase 1 (paper only).

FASE 1: apenas logging. Nenhuma ordem real até validação dos logs paper.
Roda em paralelo com current_almost_resolved sem interferência.

Uso:
    python run_reversal_sniper_paper_v1.py
    python run_reversal_sniper_paper_v1.py --seconds 3600
    python run_reversal_sniper_paper_v1.py --poll 0.5
"""
from __future__ import annotations

import argparse

from market.live_reversal_sniper_v1 import run_reversal_sniper_paper


def main() -> int:
    parser = argparse.ArgumentParser(description="Reversal Sniper — Fase 1 (paper only, zero ordens reais)")
    parser.add_argument("--seconds", type=float, default=float("inf"),
                        help="Duração máxima em segundos (padrão: até Ctrl+C)")
    parser.add_argument("--poll", type=float, default=1.0,
                        help="Intervalo de polling em segundos (padrão: 1.0)")
    args = parser.parse_args()

    print("[BOOT] Reversal Sniper v1 — Fase 1 (paper only)")
    print("[BOOT] Nenhuma ordem real será postada nesta fase.")
    print("[BOOT] Ctrl+C para encerrar.")
    print()

    run_reversal_sniper_paper(
        run_for=args.seconds,
        poll_secs=args.poll,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
