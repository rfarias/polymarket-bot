from __future__ import annotations

import math


def calculate_edge(prob_real: float, price_market: float) -> float:
    return round(prob_real - price_market, 6)


def calculate_ev(prob_real: float, price_market: float, bet_size: float) -> float:
    if price_market <= 0 or price_market >= 1:
        return 0.0
    # EV = (prob × profit_if_win) - (1-prob) × stake
    # profit_if_win = (1 - price) / price × stake  (odds implied)
    profit_if_win = (1.0 - price_market) / price_market * bet_size
    ev = prob_real * profit_if_win - (1.0 - prob_real) * bet_size
    return round(ev, 6)


def ev_per_dollar(prob_real: float, price_market: float) -> float:
    if price_market <= 0:
        return 0.0
    return round(calculate_ev(prob_real, price_market, 1.0), 6)


def should_enter(edge: float, prob_real: float, price_market: float, min_edge: float = 0.08) -> bool:
    return edge >= min_edge and ev_per_dollar(prob_real, price_market) > 0


def kelly_fraction(prob_real: float, price_market: float) -> float:
    if price_market <= 0 or price_market >= 1:
        return 0.0
    b = (1.0 - price_market) / price_market
    k = (prob_real * b - (1.0 - prob_real)) / b
    return round(max(0.0, min(k, 0.25)), 6)


def shares_for_bet(bet_size: float, price_market: float) -> float:
    if price_market <= 0:
        return 0.0
    return round(bet_size / price_market, 4)
