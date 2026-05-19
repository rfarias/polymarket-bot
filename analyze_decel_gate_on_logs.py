"""
analyze_decel_gate_on_logs.py

Análise retroativa do gate de desaceleração (Padrão 2).

Recalcula bid_velocity a partir dos snapshots existentes e avalia:
- Quantas perdas seriam evitadas com cada threshold
- Quantos wins seriam filtrados (falsos positivos)
- Tabela threshold x (losses_blocked, wins_blocked, net_ticks_saved)

Uso:
    python analyze_decel_gate_on_logs.py logs/current_almost_resolved_real_*/
    python analyze_decel_gate_on_logs.py logs/  # varre recursivamente
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Parsing de logs
# ---------------------------------------------------------------------------

def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _load_jsonl(path: Path) -> List[dict]:
    rows = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception as exc:
        print(f"[WARN] Cannot read {path}: {exc}")
    return rows


def _find_log_files(paths: List[str]) -> List[Path]:
    result = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix == ".jsonl":
            result.append(p)
        elif p.is_dir():
            result.extend(sorted(p.rglob("*.jsonl")))
    return sorted(set(result))


# ---------------------------------------------------------------------------
# Reconstrução do bid_velocity a partir dos snapshots brutos
# ---------------------------------------------------------------------------

def _reconstruct_bid_velocity(
    snapshots: List[dict],
    side: str,
    enter_ts: float,
    slug: str,
    polls_back: int = 3,
    lookback_window: int = 6,
) -> Optional[float]:
    """
    Recalcula bid_velocity para um enter_ts dado.
    Filtra por slug para evitar mistura de preços entre janelas distintas.
    Retorna velocity = price_now - price_N_polls_ago, ou None se histórico insuficiente.
    """
    price_key = "up_buy" if side == "UP" else "down_buy"
    # Coleta snapshots do mesmo slug antes do enter
    prior = [
        s for s in snapshots
        if s.get("type") == "snapshot"
        and _safe_float(s.get("ts"), 0.0) <= enter_ts
        and str(s.get("current_slug") or "") == slug
        and _safe_float(s.get("signal", {}).get(price_key), 0.0) > 0
    ]
    if len(prior) < polls_back + 1:
        return None
    # Pega os últimos lookback_window snapshots
    buf = prior[-lookback_window:]
    if len(buf) < polls_back + 1:
        return None
    price_now = _safe_float(buf[-1].get("signal", {}).get(price_key), 0.0)
    price_ago = _safe_float(buf[-(polls_back + 1)].get("signal", {}).get(price_key), 0.0)
    if price_now <= 0 or price_ago <= 0:
        return None
    return round(price_now - price_ago, 6)


# ---------------------------------------------------------------------------
# Estrutura de trade para análise
# ---------------------------------------------------------------------------

class TradeRecord:
    def __init__(self):
        self.enter_ts: float = 0.0
        self.side: str = ""
        self.setup_variant: str = ""
        self.entry_price: float = 0.0
        self.pnl_ticks: Optional[float] = None
        self.exit_reason: str = ""
        self.bid_velocity_reconstructed: Optional[float] = None
        self.bid_velocity_logged: Optional[float] = None  # se já estava no log
        self.secs_to_end: Optional[int] = None

    def is_win(self) -> bool:
        return self.pnl_ticks is not None and self.pnl_ticks > 0

    def is_loss(self) -> bool:
        return self.pnl_ticks is not None and self.pnl_ticks < 0

    def __repr__(self) -> str:
        return (
            f"Trade(side={self.side} variant={self.setup_variant} "
            f"pnl={self.pnl_ticks} vel={self.bid_velocity_reconstructed} "
            f"exit={self.exit_reason})"
        )


def _parse_trades(rows: List[dict]) -> List[TradeRecord]:
    """
    Agrupa enter + exit sequencialmente.
    Calcula PnL em ticks (tick=0.01).
    """
    trades = []
    snapshots = [r for r in rows if r.get("type") == "snapshot"]
    enters = [r for r in rows if r.get("type") == "enter"]
    exits = [r for r in rows if r.get("type") in ("exit", "flat", "redeem_flat")]

    # Para cada enter, emparelha com o exit seguinte pelo ts
    exit_idx = 0
    for ev in enters:
        t = TradeRecord()
        t.enter_ts = _safe_float(ev.get("ts"), 0.0)
        sig = ev.get("signal") or {}
        t.side = str(sig.get("side") or "")
        t.setup_variant = str(sig.get("setup_variant") or "")
        t.entry_price = _safe_float(sig.get("entry_price") or ev.get("posted_entry_price") or ev.get("signal_entry_price"), 0.0)
        t.secs_to_end = sig.get("secs_to_end")
        # Slug do evento — pode estar no signal/enter (logs novos) ou deduzido do snapshot anterior
        t_slug = str(sig.get("event_slug") or ev.get("event_slug") or sig.get("current_slug") or "")
        if not t_slug:
            # Fallback: pega o current_slug do snapshot imediatamente antes do enter
            for s in reversed(snapshots):
                if _safe_float(s.get("ts"), 0.0) <= t.enter_ts:
                    t_slug = str(s.get("current_slug") or "")
                    break

        # bid_velocity já logado (logs novos)
        decel_logged = ev.get("bid_decel_gate") or {}
        t.bid_velocity_logged = decel_logged.get("bid_velocity")

        # Emparelha com exit
        while exit_idx < len(exits) and _safe_float(exits[exit_idx].get("ts"), 0.0) < t.enter_ts:
            exit_idx += 1
        if exit_idx < len(exits):
            ex = exits[exit_idx]
            exit_price = _safe_float(ex.get("exit_price") or (ex.get("trade") or {}).get("exit_price"), 0.0)
            t.exit_reason = str(ex.get("reason") or ex.get("last_reason") or (ex.get("trade") or {}).get("last_reason") or "")
            if t.entry_price > 0 and exit_price > 0:
                t.pnl_ticks = round((exit_price - t.entry_price) / 0.01, 2)

        # Recalcula bid_velocity retroativamente (filtrado por slug)
        t.bid_velocity_reconstructed = _reconstruct_bid_velocity(
            snapshots, t.side, t.enter_ts, slug=t_slug, polls_back=3
        )

        trades.append(t)
    return trades


# ---------------------------------------------------------------------------
# Análise de thresholds
# ---------------------------------------------------------------------------

THRESHOLDS = [-0.001, -0.002, -0.003, -0.005, -0.007, -0.010, -0.015, -0.020]


def _analyze(trades: List[TradeRecord]) -> None:
    trades_with_vel = [t for t in trades if t.bid_velocity_reconstructed is not None]
    trades_without_vel = [t for t in trades if t.bid_velocity_reconstructed is None]

    wins = [t for t in trades if t.is_win()]
    losses = [t for t in trades if t.is_loss()]

    print(f"\n{'='*70}")
    print(f"ANÁLISE: GATE DE DESACELERAÇÃO (bid_velocity)")
    print(f"{'='*70}")
    print(f"Total trades: {len(trades)}  |  Wins: {len(wins)}  |  Losses: {len(losses)}")
    print(f"Trades com bid_velocity calculável: {len(trades_with_vel)}")
    print(f"Trades sem histórico suficiente: {len(trades_without_vel)}")
    print()

    # Distribuição de bid_velocity por wins e losses
    win_vels = [t.bid_velocity_reconstructed for t in wins if t.bid_velocity_reconstructed is not None]
    loss_vels = [t.bid_velocity_reconstructed for t in losses if t.bid_velocity_reconstructed is not None]

    if win_vels:
        print(f"bid_velocity WINS  — min={min(win_vels):.4f}  max={max(win_vels):.4f}  avg={sum(win_vels)/len(win_vels):.4f}")
    if loss_vels:
        print(f"bid_velocity LOSSES — min={min(loss_vels):.4f}  max={max(loss_vels):.4f}  avg={sum(loss_vels)/len(loss_vels):.4f}")

    print()
    print("Distribuição de bid_velocity em perdas:")
    buckets = [(-999, -0.020), (-0.020, -0.015), (-0.015, -0.010), (-0.010, -0.005),
               (-0.005, -0.002), (-0.002, -0.001), (-0.001, 0.0), (0.0, 999)]
    for lo, hi in buckets:
        cnt = sum(1 for v in loss_vels if lo <= v < hi)
        label = f"[{lo:+.3f}, {hi:+.3f})" if hi != 999 else f"[{lo:+.3f}, +inf)"
        if lo == -999:
            label = f"(-inf, {hi:+.3f})"
        bar = "#" * cnt
        print(f"  {label}: {cnt:3d} {bar}")

    print()
    print(f"{'Threshold':>12} | {'Losses bloq':>12} | {'Wins bloq':>10} | {'Net tk salvo':>12} | {'% losses bloq':>14} | {'% wins bloq':>11}")
    print("-" * 80)

    for thr in THRESHOLDS:
        # Bloquearia se bid_velocity < threshold (usando reconstructed)
        losses_blocked = [t for t in losses if t.bid_velocity_reconstructed is not None and t.bid_velocity_reconstructed < thr]
        wins_blocked = [t for t in wins if t.bid_velocity_reconstructed is not None and t.bid_velocity_reconstructed < thr]

        loss_ticks_saved = sum(abs(t.pnl_ticks) for t in losses_blocked if t.pnl_ticks is not None)
        win_ticks_lost = sum(t.pnl_ticks for t in wins_blocked if t.pnl_ticks is not None)
        net_ticks = loss_ticks_saved - win_ticks_lost

        pct_losses = 100 * len(losses_blocked) / len(losses) if losses else 0
        pct_wins = 100 * len(wins_blocked) / len(wins) if wins else 0

        print(
            f"  {thr:>10.3f} | {len(losses_blocked):>12} | {len(wins_blocked):>10} | "
            f"{net_ticks:>+12.1f} | {pct_losses:>13.1f}% | {pct_wins:>10.1f}%"
        )

    # Detalhe das perdas, ordenado por bid_velocity
    print()
    print("DETALHE PERDAS (bid_velocity reconstituído):")
    print(f"{'ts':>12} {'side':>5} {'variant':>28} {'vel':>8} {'pnl_tk':>8} {'secs':>5} {'exit_reason'}")
    print("-" * 90)
    loss_rows = sorted(
        [t for t in losses if t.bid_velocity_reconstructed is not None],
        key=lambda t: t.bid_velocity_reconstructed,
    )
    no_vel_losses = [t for t in losses if t.bid_velocity_reconstructed is None]
    for t in loss_rows:
        print(
            f"  {t.enter_ts:12.0f} {t.side:>5} {t.setup_variant:>28} "
            f"{t.bid_velocity_reconstructed:>+8.4f} {t.pnl_ticks:>8.1f} "
            f"{str(t.secs_to_end or '?'):>5} {t.exit_reason[:35]}"
        )
    for t in no_vel_losses:
        print(
            f"  {t.enter_ts:12.0f} {t.side:>5} {t.setup_variant:>28} "
            f"{'N/A':>8} {(t.pnl_ticks or 0.0):>8.1f} "
            f"{str(t.secs_to_end or '?'):>5} {t.exit_reason[:35]}"
        )

    # Verifica se logs novos já têm bid_velocity logado
    logged_count = sum(1 for t in trades if t.bid_velocity_logged is not None)
    if logged_count > 0:
        print(f"\n[INFO] {logged_count} trades com bid_velocity já logado diretamente (logs novos).")
        print("Comparação logged vs reconstructed:")
        for t in trades:
            if t.bid_velocity_logged is not None and t.bid_velocity_reconstructed is not None:
                diff = abs(t.bid_velocity_logged - t.bid_velocity_reconstructed)
                if diff > 0.002:
                    print(f"  DIVERGE ts={t.enter_ts:.0f} logged={t.bid_velocity_logged:.4f} recon={t.bid_velocity_reconstructed:.4f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Análise retroativa do gate de desaceleração")
    parser.add_argument("paths", nargs="*", default=["logs"], help="Arquivos JSONL ou diretórios de logs")
    parser.add_argument("--polls-back", type=int, default=3, help="Quantos polls atrás para bid_velocity (default: 3)")
    args = parser.parse_args()

    log_files = _find_log_files(args.paths)
    if not log_files:
        print(f"[ERROR] Nenhum arquivo .jsonl encontrado em: {args.paths}")
        sys.exit(1)

    print(f"[INFO] Lendo {len(log_files)} arquivo(s) de log...")
    all_rows: List[dict] = []
    for f in log_files:
        rows = _load_jsonl(f)
        # Filtra só logs do runner current_almost_resolved
        if any(r.get("type") in ("enter", "snapshot") for r in rows):
            all_rows.extend(rows)

    print(f"[INFO] {len(all_rows)} linhas carregadas")
    trades = _parse_trades(all_rows)
    _analyze(trades)


if __name__ == "__main__":
    main()
