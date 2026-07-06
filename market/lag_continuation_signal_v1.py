"""
market/lag_continuation_signal_v1.py

Setup "lag continuation": aposta que quando o BTC ja esta distante do preco de
abertura da janela de 5min (priceToBeat) e com momentum na mesma direcao perto
do fim da janela, essa tendencia tende a persistir ate a resolucao.

Portado de polymarket-overlay-indicator (src/services/lagContinuationPaperService.ts),
variante canonica "lag_continuation_30s_dom_cheap". Parametros e gate de exclusao
validados em paper la (2026-06-30 a 2026-07-06, 190 trades: WR 66.8% -> 70.2%
apos excluir a zona secs[75,90)).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class LagContinuationConfigV1:
    min_seconds_to_end: int = 30
    max_seconds_to_end: int = 120
    min_signed_distance_bps: float = -30.0
    max_signed_distance_bps: float = 30.0
    momentum_window_sec: float = 30.0
    min_momentum_bps: float = 4.0
    max_entry_ask: float = 0.70
    exit_seconds_to_end: int = 5
    # Zona morta identificada empiricamente (2026-07-06, n=22): secs 75-90 e a
    # unica faixa de secondsToEnd com PnL agregado negativo (WR 40.9%, -14.21
    # vs +372 no resto). Ver CLAUDE.md secao "Lag Continuation".
    exclude_seconds_to_end_min: int = 75
    exclude_seconds_to_end_max: int = 90

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate_lag_continuation_v1(
    *,
    secs_to_end: Optional[int],
    btc_price: Optional[float],
    price_to_beat: Optional[float],
    momentum_bps: Optional[float],
    up_ask: Optional[float],
    down_ask: Optional[float],
    cfg: LagContinuationConfigV1,
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

    entry_ask = up_ask if dominant_side == "UP" else down_ask
    if entry_ask is None or entry_ask <= 0 or entry_ask >= 1:
        return {"allow": False, "reason": "invalid_entry_ask"}
    if entry_ask > cfg.max_entry_ask:
        return {"allow": False, "reason": "entry_ask_too_expensive", "entry_ask": entry_ask}

    return {
        "allow": True,
        "side": dominant_side,
        "dominant_side": dominant_side,
        "signed_distance_bps": round(signed_distance_bps, 4),
        "price_to_beat": price_to_beat,
        "btc_price": btc_price,
        "momentum_bps": round(momentum_bps, 4),
        "directed_momentum_bps": round(directed_momentum_bps, 4),
        "entry_ask": entry_ask,
    }
