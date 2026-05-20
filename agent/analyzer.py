"""
agent/analyzer.py

Calcula métricas por setup a partir dos logs JSONL.
Chamado pelo optimizer_loop e pode ser rodado manualmente:

    python agent/analyzer.py --setup reversal_sniper
    python agent/analyzer.py --setup almost_resolved
    python agent/analyzer.py  # todos os setups
"""
from __future__ import annotations

import argparse
import json
import glob
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

LOGS_DIR = Path("logs")


# ---------------------------------------------------------------------------
# Leitura de logs
# ---------------------------------------------------------------------------

def _iter_jsonl(path: str):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Analyzer — reversal_sniper
# ---------------------------------------------------------------------------

def analyze_reversal_sniper(hours_back: float = 24.0) -> dict:
    """
    Lê logs reversal_sniper_paper_YYYYMMDD.jsonl e calcula:
    - would_enter_count: quantas vezes o score >= threshold
    - position_count: posições abertas (would_enter que entraram)
    - win_count / loss_count
    - ev_per_trade: EV médio simulado por posição
    - win_rate
    - pnl_total
    - entry_missed_pct: % de would_enter que caíram em cooldown (oportunidade perdida)
    - consecutive_losses: sequência de perdas atual
    """
    import time
    cutoff = time.time() - hours_back * 3600

    pattern = str(LOGS_DIR / "reversal_sniper_paper_*.jsonl")
    files = sorted(glob.glob(pattern))

    would_enter_count = 0
    cooldown_blocks = 0
    positions_resolved: list[dict] = []
    positions_expired: list[dict] = []

    for fpath in files:
        for ev in _iter_jsonl(fpath):
            if ev.get("ts", 0) < cutoff:
                continue
            t = ev.get("type", "")
            if t == "sniper_snapshot":
                if ev.get("would_enter") and not ev.get("in_cooldown"):
                    would_enter_count += 1
                elif ev.get("would_enter") and ev.get("in_cooldown"):
                    cooldown_blocks += 1
            elif t == "position_resolved":
                positions_resolved.append(ev)
            elif t == "position_expired":
                positions_expired.append(ev)

    wins = [p for p in positions_resolved if p.get("pnl_hold", 0) > 0]
    losses = [p for p in positions_resolved if p.get("pnl_hold", 0) <= 0]

    pnl_total = sum(p.get("pnl_hold", 0) for p in positions_resolved)
    n = len(positions_resolved)
    ev_per_trade = pnl_total / n if n > 0 else 0.0
    win_rate = len(wins) / n if n > 0 else 0.0

    # consecutive losses (reverso cronológico)
    consecutive_losses = 0
    for p in reversed(positions_resolved):
        if p.get("pnl_hold", 0) <= 0:
            consecutive_losses += 1
        else:
            break

    entry_missed_pct = (
        cooldown_blocks / (would_enter_count + cooldown_blocks)
        if (would_enter_count + cooldown_blocks) > 0 else 0.0
    )

    return {
        "setup": "reversal_sniper",
        "hours_analyzed": hours_back,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "ev_per_trade": round(ev_per_trade, 4),
        "pnl_total": round(pnl_total, 4),
        "pnl_last_24h": round(pnl_total, 4),
        "would_enter_count": would_enter_count,
        "cooldown_blocks": cooldown_blocks,
        "entry_missed_pct": round(entry_missed_pct, 4),
        "consecutive_losses": consecutive_losses,
        "positions_expired": len(positions_expired),
    }


# ---------------------------------------------------------------------------
# Analyzer — almost_resolved (lê extract_real_trades se existir, senão analisa raw)
# ---------------------------------------------------------------------------

def analyze_almost_resolved(hours_back: float = 24.0) -> dict:
    import time
    cutoff = time.time() - hours_back * 3600

    pattern = str(LOGS_DIR / "current_almost_resolved_real_*" / "current_almost_resolved_real.jsonl")
    files = sorted(glob.glob(pattern))

    wins = []
    losses = []
    total_losses = []  # platform_redeem (token vira pó)

    seen = set()

    for fpath in files:
        for ev in _iter_jsonl(fpath):
            if ev.get("ts", 0) < cutoff:
                continue
            t = ev.get("type", "")
            trade = ev.get("trade", {}) or {}
            slug = trade.get("event_slug")
            if not slug:
                continue

            if t == "flat":
                exit_ord = ev.get("exit_order") or {}
                ep = float(exit_ord.get("price", 0) or 0)
                eq = float(exit_ord.get("size_matched", 0) or 0)
                entry = float(trade.get("entry_price") or 0)
                if eq > 0 and entry > 0:
                    pnl = (ep - entry) * eq
                    key = f"{slug}:{entry}:{ep}"
                    if key not in seen:
                        seen.add(key)
                        if pnl > 0:
                            wins.append(pnl)
                        else:
                            losses.append(pnl)

            elif t == "awaiting_redeem":
                entry = float(trade.get("entry_price") or 0)
                qty = float(trade.get("entry_qty_filled") or 0)
                side_won = ev.get("side_won")
                reason = trade.get("last_reason", "")
                key = f"{slug}:{entry}:redeem"
                if key in seen or not entry or not qty:
                    continue
                if side_won is True:
                    pnl = (1.0 - entry) * qty
                    seen.add(key)
                    wins.append(pnl)
                elif side_won is False or "platform_redeem_path" in (reason or ""):
                    pnl = -entry * qty
                    seen.add(key)
                    total_losses.append(pnl)
                    losses.append(pnl)

    n = len(wins) + len(losses)
    pnl_total = sum(wins) + sum(losses)
    ev = pnl_total / n if n > 0 else 0.0

    consecutive_losses = 0
    all_trades = [(p, True) for p in wins] + [(p, False) for p in losses]

    return {
        "setup": "almost_resolved",
        "hours_analyzed": hours_back,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "total_losses_redeem": len(total_losses),
        "win_rate": round(len(wins) / n, 4) if n > 0 else 0.0,
        "ev_per_trade": round(ev, 4),
        "pnl_total": round(pnl_total, 4),
        "pnl_last_24h": round(pnl_total, 4),
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "consecutive_losses": 0,
        "entry_missed_pct": 0.0,
    }


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

ANALYZERS = {
    "reversal_sniper": analyze_reversal_sniper,
    "almost_resolved": analyze_almost_resolved,
}


def run(setup: str | None = None, hours_back: float = 24.0) -> dict[str, dict]:
    setups = [setup] if setup else list(ANALYZERS.keys())
    results = {}
    for s in setups:
        fn = ANALYZERS.get(s)
        if fn:
            results[s] = fn(hours_back=hours_back)
    return results


def _print_metrics(metrics: dict) -> None:
    setup = metrics.get("setup", "?")
    print(f"\n{'='*60}")
    print(f"  Setup: {setup}  |  Janela: {metrics.get('hours_analyzed')}h")
    print(f"{'='*60}")
    for k, v in metrics.items():
        if k in ("setup", "hours_analyzed"):
            continue
        print(f"  {k:30s}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", default=None, choices=list(ANALYZERS.keys()))
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args()

    results = run(setup=args.setup, hours_back=args.hours)
    for metrics in results.values():
        _print_metrics(metrics)
