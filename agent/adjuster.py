"""
agent/adjuster.py

Lógica de ajuste de parâmetros por setup.
Propõe um único ajuste por setup por ciclo, com reasoning em português.
Regra: no máximo 3 ajustes por ciclo total; aguardar 2h antes de
        readjustar o mesmo parâmetro.

Usado pelo optimizer_loop.py — não deve ser chamado diretamente.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

_TZ = timezone(timedelta(hours=-3))

CONFIG_PATH = Path("agent") / "config.json"
PARAM_HISTORY_PATH = Path("logs") / "param_history.jsonl"

# ---------------------------------------------------------------------------
# Ranges permitidos por setup / parâmetro
# ---------------------------------------------------------------------------

PARAM_RANGES: dict[str, dict[str, dict]] = {
    "reversal_sniper": {
        "entry_score_threshold": {"min": 2, "max": 7,  "step": 1,    "type": int},
        "hold_score_minimum":    {"min": 1, "max": 4,  "step": 1,    "type": int},
        "partial_profit_pct":    {"min": 0.3, "max": 2.0, "step": 0.1, "type": float},
        "paper_bet_size":        {"min": 10, "max": 50, "step": 5,    "type": int},
    },
    "almost_resolved": {
        "bid_decel_threshold":      {"min": -0.015, "max": -0.001, "step": 0.001, "type": float},
        "vel_range_block":          {"min": 0.02, "max": 0.10,  "step": 0.005, "type": float},
        "stop_confirmation_polls":  {"min": 1, "max": 5, "step": 1, "type": int},
        "min_active_bid_entry":     {"min": 0.85, "max": 0.96, "step": 0.01, "type": float},
    },
    "reversal_scalp": {
        "target_multiplier":        {"min": 1.2, "max": 4.0, "step": 0.1, "type": float},
        "entry_timeout_secs":       {"min": 3, "max": 20,   "step": 1,   "type": int},
        "entry_score_threshold":    {"min": 2, "max": 6,    "step": 1,   "type": int},
    },
    "market_maker": {
        "max_hold_secs":   {"min": 30, "max": 180, "step": 15, "type": int},
        "exit_before_secs":{"min": 30, "max": 90,  "step": 10, "type": int},
        "paper_bet_size":  {"min": 10, "max": 50,  "step": 5,  "type": int},
    },
    "spot_scalp": {
        "min_spot_momentum_bps":  {"min": 2.0, "max": 20.0, "step": 1.0, "type": float},
        "token_lag_threshold_bps":{"min": 1.0, "max": 10.0, "step": 0.5, "type": float},
        "max_hold_secs":          {"min": 10, "max": 60,    "step": 5,   "type": int},
        "paper_bet_size":         {"min": 10, "max": 50,    "step": 5,   "type": int},
    },
}

MIN_TRADES_TO_ADJUST = 10   # mínimo de trades antes de ajustar
MIN_SECS_BETWEEN_ADJUSTS = 7200  # 2 horas


# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _last_adjust_ts(setup: str, param: str) -> float:
    """Retorna o timestamp do último ajuste para este parâmetro."""
    try:
        with open(PARAM_HISTORY_PATH, encoding="utf-8") as f:
            last_ts = 0.0
            for line in f:
                try:
                    ev = json.loads(line.strip())
                    if ev.get("setup") == setup and ev.get("parameter") == param:
                        last_ts = max(last_ts, float(ev.get("ts", 0)))
                except Exception:
                    pass
            return last_ts
    except FileNotFoundError:
        return 0.0


def _clamp(value, rng: dict):
    """Clamp value dentro do range, arredondado para o step."""
    v = max(rng["min"], min(rng["max"], value))
    if rng["type"] is int:
        return int(round(v))
    # arredondar para step
    step = rng["step"]
    return round(round(v / step) * step, 6)


def _can_adjust(setup: str, param: str) -> bool:
    last = _last_adjust_ts(setup, param)
    return (time.time() - last) >= MIN_SECS_BETWEEN_ADJUSTS


def _record_history(setup: str, param: str, old_val, new_val, reason: str) -> None:
    ts_human = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M BRT")

    row = {
        "ts": time.time(),
        "ts_human": ts_human,
        "setup": setup,
        "parameter": param,
        "old_value": old_val,
        "value": new_val,
        "reason": reason,
    }
    PARAM_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PARAM_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Lógica de decisão por setup
# ---------------------------------------------------------------------------

def _decide_reversal_sniper(metrics: dict, cfg_setup: dict) -> Optional[dict]:
    n = metrics.get("trades", 0)
    win_rate = metrics.get("win_rate", 0.0)
    entry_missed = metrics.get("entry_missed_pct", 0.0)
    consec_losses = metrics.get("consecutive_losses", 0)
    ev = metrics.get("ev_per_trade", 0.0)
    threshold = int(cfg_setup.get("entry_score_threshold", 4))

    rng_thresh = PARAM_RANGES["reversal_sniper"]["entry_score_threshold"]

    # Pausa automática se 7+ perdas consecutivas e EV negativo
    if consec_losses >= 7 and ev < 0 and n >= MIN_TRADES_TO_ADJUST:
        if _can_adjust("reversal_sniper", "entry_score_threshold"):
            new_thresh = _clamp(threshold + 1, rng_thresh)
            if new_thresh != threshold:
                return {
                    "parameter": "entry_score_threshold",
                    "old_value": threshold,
                    "new_value": new_thresh,
                    "reasoning": (
                        f"{consec_losses} perdas consecutivas com EV negativo ({ev:.2f}/trade). "
                        f"Aumentando threshold de {threshold} para {new_thresh} para filtrar entradas piores."
                    ),
                    "expected_effect": "Reduzir frequência de entradas, evitar sequência de perdas.",
                    "decision": "adjust",
                }

    if n < MIN_TRADES_TO_ADJUST:
        return None

    # Se entry_missed alto: threshold pode estar muito alto
    if entry_missed > 0.45 and threshold > rng_thresh["min"]:
        if _can_adjust("reversal_sniper", "entry_score_threshold"):
            new_thresh = _clamp(threshold - 1, rng_thresh)
            if new_thresh != threshold:
                return {
                    "parameter": "entry_score_threshold",
                    "old_value": threshold,
                    "new_value": new_thresh,
                    "reasoning": (
                        f"entry_missed_pct={entry_missed:.0%} — mais de 45% dos sinais bloqueados por cooldown. "
                        f"Reduzindo threshold de {threshold} para {new_thresh} para capturar mais entradas."
                    ),
                    "expected_effect": "Aumentar taxa de entrada; reduzir oportunidades perdidas.",
                    "decision": "adjust",
                }

    # Se win_rate muito baixo e muitos trades: threshold muito baixo
    if win_rate < 0.15 and n >= 20 and threshold < rng_thresh["max"]:
        if _can_adjust("reversal_sniper", "entry_score_threshold"):
            new_thresh = _clamp(threshold + 1, rng_thresh)
            if new_thresh != threshold:
                return {
                    "parameter": "entry_score_threshold",
                    "old_value": threshold,
                    "new_value": new_thresh,
                    "reasoning": (
                        f"win_rate={win_rate:.0%} com {n} trades — abaixo de 15%. "
                        f"Aumentando threshold de {threshold} para {new_thresh} para filtrar entradas de menor qualidade."
                    ),
                    "expected_effect": "Melhorar win_rate reduzindo entradas de sinal fraco.",
                    "decision": "adjust",
                }

    return None


def _decide_almost_resolved(metrics: dict, cfg_setup: dict) -> Optional[dict]:
    n = metrics.get("trades", 0)
    total_loss_count = metrics.get("total_losses_redeem", 0)

    if n < MIN_TRADES_TO_ADJUST:
        return None

    # Se muitas perdas totais (platform_redeem): filtrar entradas mais cedo
    if total_loss_count / n > 0.25:
        param = "min_active_bid_entry"
        rng = PARAM_RANGES["almost_resolved"][param]
        old_val = float(cfg_setup.get(param, 0.90))
        if old_val < rng["max"] and _can_adjust("almost_resolved", param):
            new_val = _clamp(old_val + rng["step"], rng)
            if new_val != old_val:
                return {
                    "parameter": param,
                    "old_value": old_val,
                    "new_value": new_val,
                    "reasoning": (
                        f"{total_loss_count}/{n} trades ({total_loss_count/n:.0%}) resultaram em platform_redeem. "
                        f"Aumentando min_active_bid_entry de {old_val:.2f} para {new_val:.2f} "
                        f"para entrar apenas quando o winner está mais consolidado."
                    ),
                    "expected_effect": "Reduzir taxa de reversão pós-entrada; menos total_losses.",
                    "decision": "adjust",
                }

    return None


def _decide_market_maker(metrics: dict, cfg_setup: dict) -> Optional[dict]:
    n = metrics.get("trades", 0)
    win_rate = metrics.get("win_rate", 0.0)

    if n < MIN_TRADES_TO_ADJUST:
        return None

    # Se win_rate baixo: segurar mais tempo (target pode não estar sendo atingido)
    if win_rate < 0.40 and n >= 15:
        param = "max_hold_secs"
        rng = PARAM_RANGES["market_maker"][param]
        old_val = int(cfg_setup.get(param, 90))
        if old_val < rng["max"] and _can_adjust("market_maker", param):
            new_val = _clamp(old_val + rng["step"], rng)
            if new_val != old_val:
                return {
                    "parameter": param,
                    "old_value": old_val,
                    "new_value": new_val,
                    "reasoning": (
                        f"win_rate={win_rate:.0%} com {n} trades. "
                        f"Aumentando max_hold_secs de {old_val}s para {new_val}s "
                        f"para dar mais tempo ao mercado de atingir o target."
                    ),
                    "expected_effect": "Mais tempo para target hit; potencialmente mais wins.",
                    "decision": "adjust",
                }

    return None


def _decide_spot_scalp(metrics: dict, cfg_setup: dict) -> Optional[dict]:
    n = metrics.get("trades", 0)
    win_rate = metrics.get("win_rate", 0.0)

    if n < MIN_TRADES_TO_ADJUST:
        return None

    # Se win_rate baixo: aumentar filtro de momentum
    if win_rate < 0.35 and n >= 15:
        param = "min_spot_momentum_bps"
        rng = PARAM_RANGES["spot_scalp"][param]
        old_val = float(cfg_setup.get(param, 5.0))
        if old_val < rng["max"] and _can_adjust("spot_scalp", param):
            new_val = _clamp(old_val + rng["step"], rng)
            if new_val != old_val:
                return {
                    "parameter": param,
                    "old_value": old_val,
                    "new_value": new_val,
                    "reasoning": (
                        f"win_rate={win_rate:.0%} com {n} scalps. "
                        f"Aumentando min_spot_momentum_bps de {old_val:.1f} para {new_val:.1f} "
                        f"para entrar apenas em movimentos de spot mais fortes."
                    ),
                    "expected_effect": "Filtrar entradas em movimentos frágeis; melhorar direcionamento.",
                    "decision": "adjust",
                }

    return None


DECIDERS = {
    "reversal_sniper": _decide_reversal_sniper,
    "almost_resolved": _decide_almost_resolved,
    "market_maker": _decide_market_maker,
    "spot_scalp": _decide_spot_scalp,
}


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def propose_adjustments(all_metrics: dict[str, dict]) -> list[dict]:
    """
    Recebe métricas de todos os setups e propõe ajustes.
    Retorna lista de decisões (máximo 3 por chamada).
    """
    cfg = _load_config()
    decisions = []

    for setup, metrics in all_metrics.items():
        if len(decisions) >= 3:
            break
        decider = DECIDERS.get(setup)
        if not decider:
            continue
        cfg_setup = cfg.get(setup, {})
        decision = decider(metrics, cfg_setup)
        if decision:
            decision["setup"] = setup
            decisions.append(decision)

    return decisions


def apply_adjustment(decision: dict) -> None:
    """
    Aplica um ajuste ao config.json e registra no param_history.
    """
    setup = decision["setup"]
    param = decision["parameter"]
    new_val = decision["new_value"]
    old_val = decision["old_value"]
    reason = decision["reasoning"]

    cfg = _load_config()
    if setup not in cfg:
        cfg[setup] = {}
    cfg[setup][param] = new_val
    _save_config(cfg)
    _record_history(setup, param, old_val, new_val, reason)


def no_action_record(setup: str, metrics: dict, reason: str) -> dict:
    """Cria um registro de decisão de não agir."""
    return {
        "setup": setup,
        "decision": "no_action",
        "parameter": None,
        "old_value": None,
        "new_value": None,
        "reasoning": reason,
        "expected_effect": "Nenhum — aguardando mais dados ou condições melhores.",
        "metrics_snapshot": metrics,
    }
