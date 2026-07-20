"""
market/lag_continuation_btc5m_signal_v1.py

Adaptacao do setup "lag continuation" (market/lag_continuation_signal_v1.py,
validado no paper Polymarket: WR 70.2%, +$372.18/168 trades, ver CLAUDE.md)
para trade direto de BTC em candle de 5min, sem passar por um mercado binario.

Diferenca principal: no Polymarket, o "preco de entrada" e o ask de um contrato
que paga $1 se resolver a favor (ja embute o custo de execucao no book). Aqui,
o preco de entrada e o proprio spot do BTC — o custo de execucao (spread/fee)
precisa ser modelado explicitamente via fee_bps, senao o resultado simulado
fica otimista.

Mesma logica de entrada: BTC distante do preco de abertura da janela de 5min
(price_to_beat) + momentum na mesma direcao -> aposta que a tendencia
continua (LONG se dominante=UP, SHORT se dominante=DOWN).

Este modulo cobre so a ENTRADA. A saida (stop/alvo) e desacoplada do
fechamento do candle — ver market/btc5m_risk_management_v1.py — porque,
ao contrario do contrato binario Polymarket, aqui nao existe "resolucao":
a posicao pode atravessar varias janelas de 5min ate bater stop ou alvo.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class LagContinuationBTC5mConfigV1:
    min_seconds_to_end: int = 30
    max_seconds_to_end: int = 120
    min_signed_distance_bps: float = -30.0
    max_signed_distance_bps: float = 30.0
    momentum_window_sec: float = 30.0
    min_momentum_bps: float = 4.0
    # Zona morta herdada do paper Polymarket (ver CLAUDE.md, n=22, WR 40.9%
    # vs WR 70%+ no resto). Mantido como ponto de partida ate validar com
    # dados proprios deste modulo — nao remover sem nova analise.
    exclude_seconds_to_end_min: int = 75
    exclude_seconds_to_end_max: int = 90
    # Custo de execucao round-trip (entrada+saida) assumido para BTC spot/perp.
    # Nao existe no Polymarket (o book ja reflete o custo no bid/ask) — aqui
    # precisa ser subtraido explicitamente do PnL simulado.
    fee_bps_round_trip: float = 8.0

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate_lag_continuation_btc5m_v1(
    *,
    secs_to_end: Optional[int],
    btc_price: Optional[float],
    price_to_beat: Optional[float],
    momentum_bps: Optional[float],
    cfg: LagContinuationBTC5mConfigV1,
) -> dict:
    if secs_to_end is None or secs_to_end < cfg.min_seconds_to_end or secs_to_end >= cfg.max_seconds_to_end:
        return {"allow": False, "reason": "secs_out_of_range"}

    if cfg.exclude_seconds_to_end_min <= secs_to_end < cfg.exclude_seconds_to_end_max:
        return {"allow": False, "reason": "secs_dead_zone"}

    if btc_price is None or price_to_beat is None:
        return {"allow": False, "reason": "missing_btc_reference"}
    if momentum_bps is None:
        return {"allow": False, "reason": "missing_momentum"}

    signed_distance_bps = (btc_price - price_to_beat) / btc_price * 10_000
    if signed_distance_bps < cfg.min_signed_distance_bps or signed_distance_bps >= cfg.max_signed_distance_bps:
        return {"allow": False, "reason": "distance_out_of_range", "signed_distance_bps": round(signed_distance_bps, 4)}

    dominant_side = "UP" if btc_price >= price_to_beat else "DOWN"
    directed_momentum_bps = momentum_bps if dominant_side == "UP" else -momentum_bps
    if directed_momentum_bps < cfg.min_momentum_bps:
        return {"allow": False, "reason": "momentum_below_min", "directed_momentum_bps": round(directed_momentum_bps, 4)}

    position_side = "LONG" if dominant_side == "UP" else "SHORT"

    return {
        "allow": True,
        "position_side": position_side,
        "dominant_side": dominant_side,
        "signed_distance_bps": round(signed_distance_bps, 4),
        "price_to_beat": price_to_beat,
        "btc_price": btc_price,
        "momentum_bps": round(momentum_bps, 4),
        "directed_momentum_bps": round(directed_momentum_bps, 4),
        "entry_price": btc_price,
    }
