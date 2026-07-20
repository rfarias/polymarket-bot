"""
market/btc5m_risk_management_v1.py

Gestao de saida (stop-loss / take-profit) para posicoes diretas de BTC
(LONG/SHORT), desacoplada do fechamento do candle de 5min — ao contrario do
contrato binario Polymarket, aqui a posicao pode atravessar varias janelas
de 5min; ela so fecha quando o preco atinge o stop ou o alvo.

Versao inicial pedida explicitamente pelo usuario: bps fixo e simetrico dos
dois lados (stop e alvo), antes de evoluir para stop estrutural (price
action) ou baseado em ATR. Ver CLAUDE.md, secao "BTC 5min puro".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class BTC5mRiskConfigV1:
    stop_loss_bps: float = 15.0
    take_profit_bps: float = 30.0

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate_exit_v1(
    *,
    position_side: str,
    entry_price: float,
    current_price: Optional[float],
    cfg: BTC5mRiskConfigV1,
) -> dict:
    if current_price is None or entry_price <= 0:
        return {"exit": False, "move_bps": None}

    direction = 1.0 if position_side == "LONG" else -1.0
    move_bps = direction * (current_price - entry_price) / entry_price * 10_000

    if move_bps <= -cfg.stop_loss_bps:
        return {"exit": True, "reason": "stop_loss", "move_bps": round(move_bps, 4)}
    if move_bps >= cfg.take_profit_bps:
        return {"exit": True, "reason": "take_profit", "move_bps": round(move_bps, 4)}
    return {"exit": False, "move_bps": round(move_bps, 4)}
