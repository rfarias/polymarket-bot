"""
agent/optimizer_loop.py

Loop autônomo de análise e ajuste de parâmetros.

Ciclos de análise a cada 2 horas.
Relatórios markdown gerados às 06h, 12h, 18h, 23h (America/Fortaleza).

Uso:
    python agent/optimizer_loop.py
    python agent/optimizer_loop.py --interval 3600  # ciclo a cada 1h
    python agent/optimizer_loop.py --dry-run         # analisa mas não ajusta
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.analyzer import run as run_analysis
from agent.adjuster import apply_adjustment, no_action_record, propose_adjustments

try:
    import pytz
    _TZ = pytz.timezone("America/Fortaleza")
    def _now_local() -> datetime:
        return datetime.now(_TZ)
except ImportError:
    _TZ = None
    def _now_local() -> datetime:
        return datetime.utcnow()

DECISIONS_LOG = Path("logs") / "agent_decisions.jsonl"
ERRORS_LOG    = Path("logs") / "agent_errors.jsonl"
REPORTS_DIR   = Path("logs") / "reports"

REPORT_HOURS = {6, 12, 18, 23}   # horários de geração de relatório

DEFAULT_CYCLE_SECS = 7200   # 2 horas


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _ts_human() -> str:
    try:
        return _now_local().strftime("%Y-%m-%d %H:%M BRT")
    except Exception:
        return time.strftime("%Y-%m-%d %H:%M UTC")


def _log_decision(cycle: int, setup: str, metrics: dict, decision: dict, dry_run: bool) -> None:
    row = {
        "ts": time.time(),
        "ts_human": _ts_human(),
        "cycle": cycle,
        "setup": setup,
        "trades_analyzed": metrics.get("trades", 0),
        "dry_run": dry_run,
        "metrics": {
            "win_rate": metrics.get("win_rate"),
            "ev_per_trade": metrics.get("ev_per_trade"),
            "entry_missed_pct": metrics.get("entry_missed_pct"),
            "consecutive_losses": metrics.get("consecutive_losses"),
            "pnl_last_24h": metrics.get("pnl_last_24h"),
        },
        "decision": decision.get("decision", "no_action"),
        "parameter": decision.get("parameter"),
        "old_value": decision.get("old_value"),
        "new_value": decision.get("new_value"),
        "reasoning": decision.get("reasoning"),
        "expected_effect": decision.get("expected_effect"),
    }
    _append(DECISIONS_LOG, row)


# ---------------------------------------------------------------------------
# Geração de relatórios
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        with open("agent/config.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _status_str(cfg_setup: dict) -> str:
    if not cfg_setup.get("active", False):
        return "pausado"
    return cfg_setup.get("mode", "paper")


def _generate_report(cycle: int, all_metrics: dict, decisions_this_cycle: list[dict]) -> Path:
    now = _now_local()
    fname = f"report_{now.strftime('%Y%m%d_%H')}.md"
    path = REPORTS_DIR / fname
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    cfg = _load_config()
    lines = []
    lines.append(f"# Polymarket Bot — Relatório {now.strftime('%Hh')} | {now.strftime('%d/%m/%Y')}")
    lines.append("")
    lines.append(f"**Ciclo #{cycle} | {_ts_human()}**")
    lines.append("")

    # Tabela resumo
    lines.append("## Resumo por setup")
    lines.append("")
    lines.append("| Setup | Trades | WR | EV/trade | PnL | Status |")
    lines.append("|---|---|---|---|---|---|")
    for setup, m in all_metrics.items():
        cfg_s = cfg.get(setup, {})
        wr = f"{m.get('win_rate', 0):.0%}" if m.get("trades", 0) > 0 else "—"
        ev = f"${m.get('ev_per_trade', 0):+.2f}" if m.get("trades", 0) > 0 else "—"
        pnl = f"${m.get('pnl_last_24h', 0):+.2f}" if m.get("trades", 0) > 0 else "—"
        lines.append(f"| {setup} | {m.get('trades', 0)} | {wr} | {ev} | {pnl} | {_status_str(cfg_s)} |")
    lines.append("")

    # Ajustes realizados neste ciclo
    adjusts = [d for d in decisions_this_cycle if d.get("decision") == "adjust"]
    no_actions = [d for d in decisions_this_cycle if d.get("decision") == "no_action"]

    if adjusts:
        lines.append("## Ajustes realizados neste ciclo")
        lines.append("")
        for d in adjusts:
            ts = d.get("ts_human", _ts_human())
            lines.append(f"### {ts} — {d.get('setup')}")
            lines.append(f"**Parâmetro:** `{d.get('parameter')}` {d.get('old_value')} → {d.get('new_value')}")
            lines.append(f"**Motivo:** {d.get('reasoning')}")
            lines.append(f"**Efeito esperado:** {d.get('expected_effect')}")
            lines.append("")

    if no_actions:
        lines.append("## Decisões de não agir")
        lines.append("")
        for d in no_actions:
            lines.append(f"### {d.get('setup')}")
            lines.append(f"**Motivo:** {d.get('reasoning')}")
            lines.append("")

    # Parâmetros atuais
    lines.append("## Parâmetros atuais")
    lines.append("")
    cfg_fresh = _load_config()
    for setup, cfg_s in cfg_fresh.items():
        lines.append(f"### {setup} (status: {_status_str(cfg_s)})")
        for k, v in cfg_s.items():
            if not k.startswith("_"):
                lines.append(f"  - `{k}`: {v}")
        lines.append("")

    lines.append(f"*Próximo ciclo em ~{DEFAULT_CYCLE_SECS//3600}h*")

    content = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return path


def _should_report(last_report_hour: int) -> bool:
    current_hour = _now_local().hour
    return current_hour in REPORT_HOURS and current_hour != last_report_hour


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def run_optimizer(cycle_secs: float = DEFAULT_CYCLE_SECS, dry_run: bool = False) -> None:
    print(f"[OPTIMIZER] Iniciando — ciclo={cycle_secs/3600:.1f}h dry_run={dry_run}", flush=True)
    print(f"[OPTIMIZER] Log: {DECISIONS_LOG}", flush=True)
    if dry_run:
        print("[OPTIMIZER] MODO DRY-RUN: métricas serão analisadas mas nenhum ajuste será aplicado.", flush=True)

    _append(DECISIONS_LOG, {
        "ts": time.time(),
        "ts_human": _ts_human(),
        "cycle": 0,
        "event": "optimizer_start",
        "dry_run": dry_run,
        "cycle_secs": cycle_secs,
    })

    cycle = 0
    last_report_hour = -1

    while True:
        cycle += 1
        cycle_start = time.time()
        print(f"\n[OPTIMIZER] === Ciclo #{cycle} | {_ts_human()} ===", flush=True)

        try:
            # 1. Análise de métricas
            all_metrics = run_analysis(hours_back=24.0)
            for setup, m in all_metrics.items():
                print(f"  {setup}: trades={m.get('trades', 0)} "
                      f"wr={m.get('win_rate', 0):.0%} "
                      f"ev={m.get('ev_per_trade', 0):+.4f} "
                      f"pnl={m.get('pnl_last_24h', 0):+.2f}", flush=True)

            # 2. Propor ajustes
            decisions_this_cycle: list[dict] = []
            proposed = propose_adjustments(all_metrics)

            # Setups sem proposta → no_action
            for setup, metrics in all_metrics.items():
                has_proposal = any(d.get("setup") == setup for d in proposed)
                if not has_proposal:
                    n = metrics.get("trades", 0)
                    if n < 10:
                        reason = f"Apenas {n} trades nas últimas 24h — insuficiente para ajustar (mínimo: 10)."
                    else:
                        reason = "Métricas dentro do esperado para este estágio. Nenhum ajuste necessário."
                    d = no_action_record(setup, metrics, reason)
                    d["setup"] = setup
                    decisions_this_cycle.append(d)
                    _log_decision(cycle, setup, metrics, d, dry_run)

            # Registrar e aplicar propostas
            for d in proposed:
                setup = d["setup"]
                metrics = all_metrics.get(setup, {})
                d["ts_human"] = _ts_human()
                decisions_this_cycle.append(d)
                _log_decision(cycle, setup, metrics, d, dry_run)

                if not dry_run:
                    apply_adjustment(d)
                    print(f"  AJUSTE {setup}.{d['parameter']}: "
                          f"{d['old_value']} → {d['new_value']}", flush=True)
                    print(f"  Motivo: {d['reasoning'][:80]}...", flush=True)
                else:
                    print(f"  [DRY-RUN] Proposta: {setup}.{d['parameter']}: "
                          f"{d['old_value']} → {d['new_value']}", flush=True)

            # 3. Relatório se estiver no horário certo
            if _should_report(last_report_hour):
                report_path = _generate_report(cycle, all_metrics, decisions_this_cycle)
                last_report_hour = _now_local().hour
                print(f"  Relatório gerado: {report_path}", flush=True)

        except Exception as e:
            err = {"ts": time.time(), "cycle": cycle, "error": str(e)}
            _append(ERRORS_LOG, err)
            print(f"  [ERRO] {e}", flush=True)

        # Aguardar até o próximo ciclo
        elapsed = time.time() - cycle_start
        sleep_secs = max(0, cycle_secs - elapsed)
        print(f"[OPTIMIZER] Próximo ciclo em {sleep_secs/3600:.1f}h ({sleep_secs:.0f}s)", flush=True)
        time.sleep(sleep_secs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=DEFAULT_CYCLE_SECS,
                        help="Intervalo entre ciclos em segundos (padrão: 7200)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analisa mas não aplica ajustes")
    args = parser.parse_args()
    run_optimizer(cycle_secs=args.interval, dry_run=args.dry_run)
