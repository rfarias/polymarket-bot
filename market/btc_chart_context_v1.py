"""
BTC chart context for trade entry filtering.

Fetches 5min (and optionally 15min) Binance OHLCV candles and computes:
  - EMA9 / EMA21 trend bias
  - Support / Resistance proximity via pivot highs and lows
  - Last closed candle pattern (hammer, shooting star, engulfing, etc.)
  - 15min alignment check (5min slot ends at a 15min boundary)

The result is a BtcChartContext whose get_penalty_for_side(side) method
returns a float in [0.0, 1.0]:
  0.0 = with-trend or neutral (no extra penalty)
  1.0 = strongly counter-trend (maximum penalty)
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple


BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_CACHE_TTL_SECS = 60.0
_cache: dict = {}


# ---------------------------------------------------------------------------
# Internal candle type
# ---------------------------------------------------------------------------

@dataclass
class _Candle:
    open_time: int   # Unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

@dataclass
class BtcChartContext:
    trend_5m: str              # "UP" | "DOWN" | "NEUTRAL"
    ema9_5m: float
    ema21_5m: float
    at_resistance: bool
    at_support: bool
    resistance_level: Optional[float]
    support_level: Optional[float]
    last_candle_pattern: str   # e.g. "hammer", "shooting_star", "doji", ...
    last_candle_bias: float    # -1.0 (bearish) … +1.0 (bullish)
    is_15m_aligned: bool
    trend_15m: Optional[str]   # set only when is_15m_aligned
    ema9_15m: Optional[float]
    ema21_15m: Optional[float]
    btc_price: float
    timestamp: float
    fetch_error: bool = False

    def get_penalty_for_side(self, side: str) -> float:
        """
        Soft-filter penalty for entering on this side.
        Returns 0.0 when with-trend or neutral; up to 1.0 when strongly counter-trend.
        Always returns 0.0 when fetch_error is True (no data = no penalty).
        """
        if self.fetch_error:
            return 0.0

        penalty = 0.0

        if side == "UP":
            if self.trend_5m == "DOWN":
                penalty += 0.50
            elif self.trend_5m == "NEUTRAL":
                penalty += 0.10
            if self.last_candle_bias < -0.50:
                penalty += 0.30
            elif self.last_candle_bias < -0.20:
                penalty += 0.15
            if self.at_resistance:
                penalty += 0.20
            if self.is_15m_aligned and self.trend_15m == "DOWN":
                penalty += 0.30
        elif side == "DOWN":
            if self.trend_5m == "UP":
                penalty += 0.50
            elif self.trend_5m == "NEUTRAL":
                penalty += 0.10
            if self.last_candle_bias > 0.50:
                penalty += 0.30
            elif self.last_candle_bias > 0.20:
                penalty += 0.15
            if self.at_support:
                penalty += 0.20
            if self.is_15m_aligned and self.trend_15m == "UP":
                penalty += 0.30

        return min(1.0, penalty)

    def summary(self) -> dict:
        return {
            "trend_5m": self.trend_5m,
            "ema9_5m": round(self.ema9_5m, 2),
            "ema21_5m": round(self.ema21_5m, 2),
            "at_resistance": self.at_resistance,
            "at_support": self.at_support,
            "resistance_level": round(self.resistance_level, 2) if self.resistance_level else None,
            "support_level": round(self.support_level, 2) if self.support_level else None,
            "last_candle_pattern": self.last_candle_pattern,
            "last_candle_bias": round(self.last_candle_bias, 3),
            "is_15m_aligned": self.is_15m_aligned,
            "trend_15m": self.trend_15m,
            "fetch_error": self.fetch_error,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ema(closes: List[float], period: int) -> List[float]:
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(closes[:period]) / period]
    for c in closes[period:]:
        result.append(c * k + result[-1] * (1.0 - k))
    return result


def _compute_trend(candles: List[_Candle]) -> Tuple[str, float, float]:
    """Returns (trend, ema9, ema21). trend in UP / DOWN / NEUTRAL."""
    closes = [c.close for c in candles]
    ema9_vals = _ema(closes, 9)
    ema21_vals = _ema(closes, 21)

    if not ema9_vals or not ema21_vals:
        last = closes[-1] if closes else 0.0
        return "NEUTRAL", last, last

    ema9 = ema9_vals[-1]
    ema21 = ema21_vals[-1]
    gap_bps = (ema9 - ema21) / ema21 * 10_000.0 if ema21 > 0 else 0.0

    if gap_bps > 3.0:
        trend = "UP"
    elif gap_bps < -3.0:
        trend = "DOWN"
    else:
        trend = "NEUTRAL"

    return trend, ema9, ema21


def _detect_pattern(candles: List[_Candle]) -> Tuple[str, float]:
    """
    Analyse the last closed candle (candles[-1]) and the one before it.
    Returns (pattern_name, bias) where bias is -1.0 (bearish) to +1.0 (bullish).
    """
    if len(candles) < 2:
        return "neutral", 0.0

    c = candles[-1]
    prev = candles[-2]

    body = abs(c.close - c.open)
    total_range = c.high - c.low

    if total_range < 1e-9:
        return "neutral", 0.0

    upper_shadow = c.high - max(c.open, c.close)
    lower_shadow = min(c.open, c.close) - c.low
    body_ratio = body / total_range
    is_bull = c.close >= c.open

    # Doji — open ≈ close, very small body
    if body_ratio < 0.10:
        return "doji", 0.0

    # Hammer — small body near top, long lower shadow
    if body_ratio < 0.35 and lower_shadow >= 2.0 * body and upper_shadow <= 0.5 * body:
        return "hammer", +0.65

    # Shooting star — small body near bottom, long upper shadow
    if body_ratio < 0.35 and upper_shadow >= 2.0 * body and lower_shadow <= 0.5 * body:
        return "shooting_star", -0.65

    # Engulfing
    prev_body = abs(prev.close - prev.open)
    if prev_body > 0:
        prev_is_bull = prev.close >= prev.open
        if not prev_is_bull and is_bull and body > prev_body * 1.1:
            return "bullish_engulfing", +0.80
        if prev_is_bull and not is_bull and body > prev_body * 1.1:
            return "bearish_engulfing", -0.80

    # Strong directional candle
    if body_ratio > 0.70:
        return ("strong_bull", +0.50) if is_bull else ("strong_bear", -0.50)

    # Pin bar
    body_mid = (c.open + c.close) / 2.0
    candle_mid = (c.high + c.low) / 2.0
    if lower_shadow > 0.60 * total_range and body_mid > candle_mid:
        return "pin_bar_bull", +0.45
    if upper_shadow > 0.60 * total_range and body_mid < candle_mid:
        return "pin_bar_bear", -0.45

    # Mild directional
    return ("neutral", +0.15) if is_bull else ("neutral", -0.15)


def _find_sr_levels(
    candles: List[_Candle],
    current_price: float,
    proximity_bps: float = 15.0,
    pivot_n: int = 2,
) -> Tuple[bool, bool, Optional[float], Optional[float]]:
    """
    Identify pivot highs/lows from the closed candles (excluding the current one).
    Returns (at_resistance, at_support, resistance_level, support_level).
    """
    refs = candles[:-1]  # exclude current open candle
    if len(refs) < pivot_n * 2 + 1:
        return False, False, None, None

    threshold = current_price * proximity_bps / 10_000.0
    pivot_highs: List[float] = []
    pivot_lows: List[float] = []

    for i in range(pivot_n, len(refs) - pivot_n):
        h = refs[i].high
        lo = refs[i].low
        if all(h >= refs[j].high for j in range(i - pivot_n, i + pivot_n + 1) if j != i):
            pivot_highs.append(h)
        if all(lo <= refs[j].low for j in range(i - pivot_n, i + pivot_n + 1) if j != i):
            pivot_lows.append(lo)

    at_resistance = False
    resistance_level: Optional[float] = None
    best_res_dist = float("inf")
    for lv in pivot_highs:
        dist = lv - current_price
        if 0 < dist <= threshold and dist < best_res_dist:
            at_resistance = True
            resistance_level = lv
            best_res_dist = dist

    at_support = False
    support_level: Optional[float] = None
    best_sup_dist = float("inf")
    for lv in pivot_lows:
        dist = current_price - lv
        if 0 < dist <= threshold and dist < best_sup_dist:
            at_support = True
            support_level = lv
            best_sup_dist = dist

    return at_resistance, at_support, resistance_level, support_level


def _fetch_klines(symbol: str, interval: str, end_ms: int, limit: int) -> List[_Candle]:
    url = (
        f"{BINANCE_KLINES_URL}?symbol={symbol}&interval={interval}"
        f"&endTime={end_ms}&limit={limit}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = json.loads(resp.read())
    return [
        _Candle(
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in raw
    ]


def _is_15m_aligned(event_end_ts: float) -> bool:
    """True when the 5min event slot ends exactly at a 15min boundary."""
    return int(event_end_ts) % 900 == 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_btc_chart_context(
    current_price: float,
    entry_ts: float,
    event_end_ts: float,
    *,
    symbol: str = "BTCUSDT",
    end_ms_override: Optional[int] = None,
) -> BtcChartContext:
    """
    Fetch BTC chart context for a trade entry attempt.

    Parameters
    ----------
    current_price : float
        Current BTC/USD price (used for S/R proximity).
    entry_ts : float
        Unix timestamp of the entry attempt (for cache key).
    event_end_ts : float
        Unix timestamp when the Polymarket 5min event resolves.
        Used to check 15min alignment.
    symbol : str
        Binance symbol, default "BTCUSDT".
    end_ms_override : int, optional
        If set, klines are fetched ending at this Unix-ms timestamp.
        Use for historical replay. Otherwise the current 5min open is used.
    """
    cache_bucket = int(entry_ts / 60)
    cache_key = f"{cache_bucket}_{end_ms_override or 0}"
    cached = _cache.get(cache_key)
    if cached and (entry_ts - cached["ts"]) < _CACHE_TTL_SECS:
        return cached["ctx"]

    try:
        # endTime: last ms before the current 5min candle opened → last closed candle
        if end_ms_override is not None:
            end_5m_ms = end_ms_override
        else:
            current_5m_open_sec = int(entry_ts / 300) * 300
            end_5m_ms = current_5m_open_sec * 1000 - 1  # ms, excludes current open candle

        candles_5m = _fetch_klines(symbol, "5m", end_5m_ms, 25)

        trend_5m, ema9_5m, ema21_5m = _compute_trend(candles_5m)
        last_pattern, last_bias = _detect_pattern(candles_5m)
        at_res, at_sup, res_lv, sup_lv = _find_sr_levels(candles_5m, current_price)

        is_15m = _is_15m_aligned(event_end_ts)
        trend_15m: Optional[str] = None
        ema9_15m: Optional[float] = None
        ema21_15m: Optional[float] = None

        if is_15m:
            candles_15m = _fetch_klines(symbol, "15m", end_5m_ms, 15)
            if candles_15m:
                trend_15m, ema9_15m, ema21_15m = _compute_trend(candles_15m)

        ctx = BtcChartContext(
            trend_5m=trend_5m,
            ema9_5m=ema9_5m,
            ema21_5m=ema21_5m,
            at_resistance=at_res,
            at_support=at_sup,
            resistance_level=res_lv,
            support_level=sup_lv,
            last_candle_pattern=last_pattern,
            last_candle_bias=last_bias,
            is_15m_aligned=is_15m,
            trend_15m=trend_15m,
            ema9_15m=ema9_15m,
            ema21_15m=ema21_15m,
            btc_price=current_price,
            timestamp=entry_ts,
            fetch_error=False,
        )

    except Exception:
        ctx = BtcChartContext(
            trend_5m="NEUTRAL",
            ema9_5m=current_price,
            ema21_5m=current_price,
            at_resistance=False,
            at_support=False,
            resistance_level=None,
            support_level=None,
            last_candle_pattern="neutral",
            last_candle_bias=0.0,
            is_15m_aligned=False,
            trend_15m=None,
            ema9_15m=None,
            ema21_15m=None,
            btc_price=current_price,
            timestamp=entry_ts,
            fetch_error=True,
        )

    _cache[cache_key] = {"ts": entry_ts, "ctx": ctx}
    return ctx
