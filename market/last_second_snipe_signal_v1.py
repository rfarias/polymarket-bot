"""
market/last_second_snipe_signal_v1.py

Last-second snipe signal module:
  - Clock-based market discovery (Requisito 1)
  - Window delta as dominant indicator (Requisito 3)
  - 7-indicator composite score (Requisito 4)
  - Token price model for realistic backtesting (Requisito 6)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from market.binance_ws_v1 import (
    BinanceTickFeed,
    fetch_binance_price_at,
    fetch_binance_klines_recent,
)
from market.slug_discovery import fetch_event_by_slug


# ---------------------------------------------------------------------------
# Market discovery (Requisito 1)
# ---------------------------------------------------------------------------

def current_window_ts(now: Optional[float] = None) -> int:
    """Unix timestamp of the start of the current 5-min BTC window."""
    t = int(now or time.time())
    return t - (t % 300)


def window_slug(window_ts: int) -> str:
    return f"btc-updown-5m-{window_ts}"


@dataclass
class SnipeMarket:
    slug: str
    window_ts: int
    close_time: int          # = window_ts + 300
    up_token_id: str
    down_token_id: str
    up_price: float          # current ask / market price
    down_price: float
    condition_id: str = ""
    active: bool = True


def fetch_snipe_market(window_ts: Optional[int] = None) -> Optional[SnipeMarket]:
    """
    Fetch the current (or given) 5-min BTC window from Gamma API.
    Returns None if market not found or inactive.
    """
    ts = window_ts or current_window_ts()
    slug = window_slug(ts)
    event = fetch_event_by_slug(slug)
    if not event:
        return None

    markets = event.get("markets") or []
    if not markets:
        return None
    m = markets[0]

    token_ids = m.get("clobTokenIds") or []
    outcomes = m.get("outcomes") or []
    prices_raw = m.get("outcomePrices") or []

    if len(token_ids) < 2 or len(outcomes) < 2:
        return None

    up_idx = 0
    down_idx = 1
    if outcomes[0].lower() != "up":
        up_idx, down_idx = down_idx, up_idx

    prices = [float(p) for p in prices_raw] if prices_raw else [0.5, 0.5]

    return SnipeMarket(
        slug=slug,
        window_ts=ts,
        close_time=ts + 300,
        up_token_id=str(token_ids[up_idx]),
        down_token_id=str(token_ids[down_idx]),
        up_price=prices[up_idx] if len(prices) > up_idx else 0.5,
        down_price=prices[down_idx] if len(prices) > down_idx else 0.5,
        condition_id=str(m.get("conditionId") or ""),
        active=bool(m.get("active", True)),
    )


# ---------------------------------------------------------------------------
# Window delta (Requisito 3)
# ---------------------------------------------------------------------------

def window_delta_pct(current_price: float, window_open_price: float) -> float:
    """Returns (current - open) / open * 100 as a percentage."""
    if window_open_price <= 0:
        return 0.0
    return (current_price - window_open_price) / window_open_price * 100.0


def window_delta_weight(delta_pct: float) -> Tuple[float, float]:
    """
    Returns (weight, abs_delta_pct).
    Weight is 5–7 based on magnitude of move vs window open.
    """
    abs_d = abs(delta_pct)
    if abs_d >= 0.10:
        return 7.0, abs_d
    elif abs_d >= 0.02:
        return 5.0, abs_d
    elif abs_d >= 0.005:
        return 3.0, abs_d
    elif abs_d >= 0.001:
        return 1.0, abs_d
    else:
        return 0.0, abs_d


# ---------------------------------------------------------------------------
# Indicator helpers (Requisito 4)
# ---------------------------------------------------------------------------

def _ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1 + rs)


# ---------------------------------------------------------------------------
# 7-indicator composite signal (Requisito 4)
# ---------------------------------------------------------------------------

@dataclass
class SnipeSignal:
    side: str                    # "UP" or "DOWN"
    score: float                 # raw composite score (positive=UP, negative=DOWN)
    confidence: float            # 0.0–1.0
    window_delta_pct: float
    window_delta_weight: float
    micro_momentum: float        # indicator 2
    acceleration: float          # indicator 3
    ema_cross: float             # indicator 4
    rsi_contribution: float      # indicator 5
    volume_surge: float          # indicator 6
    tick_trend: float            # indicator 7
    indicators: dict = field(default_factory=dict)
    timestamp: float = 0.0


def compute_snipe_signal(
    tick_feed: BinanceTickFeed,
    window_open_price: float,
    *,
    candles_1m: Optional[List[dict]] = None,
) -> SnipeSignal:
    """
    Compute the 7-indicator composite snipe signal.

    Parameters
    ----------
    tick_feed : BinanceTickFeed
        Live tick feed (must be running).
    window_open_price : float
        BTC price at the start of the 5-min window.
    candles_1m : list of dicts, optional
        Pre-fetched 1m candles. If None, fetched fresh from Binance REST.
    """
    now = time.time()
    current_price = tick_feed.current_price()
    if current_price <= 0:
        current_price = window_open_price  # fallback

    # ---- Fetch candles if needed ----
    if candles_1m is None:
        candles_1m = fetch_binance_klines_recent("BTCUSDT", "1m", 30)

    closes = [c["close"] for c in candles_1m] if candles_1m else []
    volumes = [c["volume"] for c in candles_1m] if candles_1m else []

    score = 0.0

    # ---- Indicator 1: Window Delta (weight 5–7) ----
    delta_pct = window_delta_pct(current_price, window_open_price)
    w_delta, _ = window_delta_weight(delta_pct)
    direction_sign = 1.0 if delta_pct >= 0 else -1.0
    ind1_contrib = direction_sign * w_delta
    score += ind1_contrib

    # ---- Indicator 2: Micro Momentum (weight 2) ----
    # Direction of last 2 closed 1m candles
    micro_mom = 0.0
    if len(closes) >= 2:
        if closes[-1] > closes[-2]:
            micro_mom = 1.0
        elif closes[-1] < closes[-2]:
            micro_mom = -1.0
        if len(closes) >= 3:
            if closes[-2] > closes[-3]:
                micro_mom += 0.5
            elif closes[-2] < closes[-3]:
                micro_mom -= 0.5
    ind2_contrib = micro_mom * 2.0
    score += ind2_contrib

    # ---- Indicator 3: Acceleration (weight 1.5) ----
    # Is recent momentum growing or declining?
    acceleration = 0.0
    if len(closes) >= 4:
        m1 = closes[-1] - closes[-2]   # last candle move
        m2 = closes[-2] - closes[-3]   # prior candle move
        if m1 * m2 > 0:                # same direction
            acceleration = 1.0 if abs(m1) > abs(m2) else 0.5
            acceleration *= (1.0 if m1 > 0 else -1.0)
        elif m1 * m2 < 0:              # deceleration / reversal
            acceleration = -0.5 * (1.0 if m1 > 0 else -1.0)
    ind3_contrib = acceleration * 1.5
    score += ind3_contrib

    # ---- Indicator 4: EMA 9/21 Crossover (weight 1) ----
    ema_cross = 0.0
    if len(closes) >= 21:
        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        if ema9[-1] > ema21[-1]:
            ema_cross = 1.0
        elif ema9[-1] < ema21[-1]:
            ema_cross = -1.0
    ind4_contrib = ema_cross * 1.0
    score += ind4_contrib

    # ---- Indicator 5: RSI (weight 1–2) ----
    rsi_contrib = 0.0
    rsi_val = _rsi(closes, 14) if len(closes) >= 15 else 50.0
    if rsi_val > 75:
        rsi_contrib = 2.0   # overbought → confirms UP momentum
    elif rsi_val > 55:
        rsi_contrib = 1.0
    elif rsi_val < 25:
        rsi_contrib = -2.0  # oversold → confirms DOWN momentum
    elif rsi_val < 45:
        rsi_contrib = -1.0
    score += rsi_contrib

    # ---- Indicator 6: Volume Surge (weight 1) ----
    volume_surge = 0.0
    if len(volumes) >= 6:
        recent_avg = sum(volumes[-3:]) / 3
        prior_avg = sum(volumes[-6:-3]) / 3
        if prior_avg > 0 and recent_avg >= prior_avg * 1.5:
            volume_surge = direction_sign * 1.0  # confirm direction on surge
    ind6_contrib = volume_surge
    score += ind6_contrib

    # ---- Indicator 7: Tick Trend (weight 2) ----
    tick_score = tick_feed.tick_direction_score(n_secs=8.0)
    ind7_contrib = tick_score * 2.0
    score += ind7_contrib

    # ---- Composite ----
    side = "UP" if score >= 0 else "DOWN"
    confidence = min(abs(score) / 7.0, 1.0)

    return SnipeSignal(
        side=side,
        score=score,
        confidence=confidence,
        window_delta_pct=delta_pct,
        window_delta_weight=w_delta,
        micro_momentum=micro_mom,
        acceleration=acceleration,
        ema_cross=ema_cross,
        rsi_contribution=rsi_contrib,
        volume_surge=volume_surge,
        tick_trend=tick_score,
        indicators={
            "ind1_window_delta": ind1_contrib,
            "ind2_micro_momentum": ind2_contrib,
            "ind3_acceleration": ind3_contrib,
            "ind4_ema_cross": ind4_contrib,
            "ind5_rsi": rsi_contrib,
            "ind6_volume_surge": ind6_contrib,
            "ind7_tick_trend": ind7_contrib,
            "rsi_val": round(rsi_val, 1),
            "current_price": current_price,
            "window_open_price": window_open_price,
        },
        timestamp=now,
    )


# ---------------------------------------------------------------------------
# Token pricing model (Requisito 6)
# ---------------------------------------------------------------------------

def estimate_token_price(delta_pct: float) -> float:
    """
    Estimate the winning token price based on window delta percentage.
    Piecewise model calibrated from live observation.
    delta_pct is the absolute value of % move vs window open.
    """
    d = abs(delta_pct)
    if d < 0.005:
        return 0.50
    elif d < 0.02:
        # linear 0.50 → 0.55
        return 0.50 + (d - 0.005) / (0.02 - 0.005) * 0.05
    elif d < 0.05:
        # linear 0.55 → 0.65
        return 0.55 + (d - 0.02) / (0.05 - 0.02) * 0.10
    elif d < 0.10:
        # linear 0.65 → 0.80
        return 0.65 + (d - 0.05) / (0.10 - 0.05) * 0.15
    elif d < 0.15:
        # linear 0.80 → 0.92
        return 0.80 + (d - 0.10) / (0.15 - 0.10) * 0.12
    else:
        return min(0.97, 0.92 + (d - 0.15) * 0.5)


def token_price_from_market(market: SnipeMarket, side: str) -> float:
    """Use live Polymarket price if available, else estimate from delta."""
    if side == "UP":
        return market.up_price if market.up_price > 0 else 0.5
    return market.down_price if market.down_price > 0 else 0.5
