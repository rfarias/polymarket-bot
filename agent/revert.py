"""
agent/revert.py

Script de reversão manual de parâmetros.

Uso:
    python agent/revert.py --setup reversal_sniper --param entry_score_threshold
    python agent/revert.py --list reversal_sniper   # mostra histórico do setup
    python agent/revert.py --list-all               # mostra todos os parâmetros já alterados
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

PARAM_HISTORY_PATH = Path("logs") / "param_history.jsonl"
DECISIONS_LOG      = Path("logs") / "agent_decisions.jsonl"
CONFIG_PATH        = Path("agent") / "config.json"


def _load_history() -> list[dict]:
    rows = []
    try:
        with open(PARAM_HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return rows


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


_TZ = timezone(timedelta(hours=-3))

def _ts_human() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M BRT")


def cmd_list_all(history: list[dict]) -> None:
    if not history:
        print("Nenhum ajuste registrado ainda.")
        return
    # agrupar por setup/param
    params: dict[str, list] = {}
    for ev in history:
        key = f"{ev.get('setup')}.{ev.get('parameter')}"
        params.setdefault(key, []).append(ev)

    for key, evs in sorted(params.items()):
        print(f"\n{'─'*55}")
        print(f"  {key}")
        print(f"{'─'*55}")
        for ev in evs:
            ts = ev.get("ts_human") or time.strftime("%Y-%m-%d %H:%M", time.localtime(ev.get("ts", 0)))
            old = ev.get("old_value", "?")
            val = ev.get("value", "?")
            print(f"  [{ts}]  {old} -> {val}  |  {ev.get('reason', '')[:60]}")


def cmd_list_setup(setup: str, history: list[dict]) -> None:
    filtered = [ev for ev in history if ev.get("setup") == setup]
    if not filtered:
        print(f"Nenhum ajuste registrado para '{setup}'.")
        return
    # agrupar por param
    params: dict[str, list] = {}
    for ev in filtered:
        params.setdefault(ev.get("parameter", "?"), []).append(ev)

    for param, evs in sorted(params.items()):
        print(f"\n{'─'*55}")
        print(f"  {setup}.{param}")
        print(f"{'─'*55}")
        for i, ev in enumerate(evs):
            ts = ev.get("ts_human") or time.strftime("%Y-%m-%d %H:%M", time.localtime(ev.get("ts", 0)))
            old = ev.get("old_value", "?")
            val = ev.get("value", "?")
            print(f"  [{i}] [{ts}]  {old} -> {val}  |  {ev.get('reason', '')[:60]}")


def cmd_revert(setup: str, param: str, history: list[dict]) -> None:
    filtered = [ev for ev in history if ev.get("setup") == setup and ev.get("parameter") == param]
    if not filtered:
        print(f"Nenhum ajuste encontrado para {setup}.{param}")
        sys.exit(1)

    print(f"\nHistórico de {setup}.{param}:")
    print(f"{'─'*55}")
    for i, ev in enumerate(filtered):
        ts = ev.get("ts_human") or time.strftime("%Y-%m-%d %H:%M", time.localtime(ev.get("ts", 0)))
        old = ev.get("old_value", "?")
        val = ev.get("value", "?")
        print(f"  [{i}] [{ts}]  {old} -> {val}")
        if ev.get("reason"):
            print(f"       Motivo: {ev['reason'][:80]}")

    cfg = _load_config()
    current = cfg.get(setup, {}).get(param, "?")
    print(f"\nValor atual: {current}")

    if len(filtered) == 1:
        # Só um registro: reverter para o old_value desse registro
        target = filtered[0].get("old_value")
        print(f"Único ajuste encontrado. Revertendo para: {target}")
        choice_idx = 0
        revert_val = target
    else:
        print(f"\nDigite o índice [0-{len(filtered)-1}] para reverter para o valor ANTES desse ajuste,")
        print("ou 'q' para cancelar: ", end="")
        try:
            raw = input().strip()
        except EOFError:
            raw = "q"
        if raw.lower() == "q":
            print("Cancelado.")
            sys.exit(0)
        try:
            choice_idx = int(raw)
            if not 0 <= choice_idx < len(filtered):
                raise ValueError()
        except ValueError:
            print("Índice inválido. Abortando.")
            sys.exit(1)

        revert_val = filtered[choice_idx].get("old_value")

    # Aplicar reversão
    if setup not in cfg:
        cfg[setup] = {}
    cfg[setup][param] = revert_val
    _save_config(cfg)

    # Registrar no param_history
    row = {
        "ts": time.time(),
        "ts_human": _ts_human(),
        "setup": setup,
        "parameter": param,
        "old_value": current,
        "value": revert_val,
        "reason": "manual_revert via agent/revert.py",
    }
    with open(PARAM_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # Registrar no decisions log
    audit = {
        "ts": time.time(),
        "ts_human": _ts_human(),
        "cycle": -1,
        "setup": setup,
        "decision": "manual_revert",
        "parameter": param,
        "old_value": current,
        "new_value": revert_val,
        "reasoning": f"Reversão manual solicitada pelo usuário. Valor anterior ao ciclo [{choice_idx if len(filtered) > 1 else 0}] restaurado.",
        "expected_effect": "Retornar ao comportamento anterior ao ajuste revertido.",
    }
    DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(DECISIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(audit, ensure_ascii=False) + "\n")

    print(f"\nRevertido: {setup}.{param} = {current} -> {revert_val}")
    print("Config.json atualizado. Registrado no audit log.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Reverte parâmetros para valores anteriores."
    )
    parser.add_argument("--setup", help="Nome do setup (ex: reversal_sniper)")
    parser.add_argument("--param", help="Nome do parâmetro (ex: entry_score_threshold)")
    parser.add_argument("--list", metavar="SETUP", help="Lista histórico de um setup")
    parser.add_argument("--list-all", action="store_true", help="Lista todos os ajustes")
    args = parser.parse_args()

    history = _load_history()

    if args.list_all:
        cmd_list_all(history)
    elif args.list:
        cmd_list_setup(args.list, history)
    elif args.setup and args.param:
        cmd_revert(args.setup, args.param, history)
    else:
        parser.print_help()
        sys.exit(1)
