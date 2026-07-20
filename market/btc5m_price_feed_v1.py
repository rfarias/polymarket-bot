"""
market/btc5m_price_feed_v1.py

Feed de preco para o modulo BTC 5min "puro" (sem Polymarket) — le candles de
5 minutos direto da Binance, alinhados ao relogio UTC (00:00, 00:05, 00:10, ...),
em vez de depender do eventStartTime de um mercado binario.

Reaproveita fetch_external_btc_reference_v1 (mediana Binance/Coinbase) de
market/current_scalp_signal_v1.py como referencia de preco corrente.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from market.current_scalp_signal_v1 import fetch_external_btc_reference_v1

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
WINDOW_SECONDS = 300
TIMEOUT = 10


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


@dataclass
class Window5mV1:
    window_start_ts: float
    window_end_ts: float
    open_price: Optional[float]
    ok: bool
    reason: str


def current_window_bounds_v1(now: Optional[float] = None) -> tuple[float, float]:
    import time as _time

    now = _time.time() if now is None else now
    window_start = (int(now) // WINDOW_SECONDS) * WINDOW_SECONDS
    return float(window_start), float(window_start + WINDOW_SECONDS)


def fetch_current_5m_window_v1(now: Optional[float] = None) -> Window5mV1:
    window_start_ts, window_end_ts = current_window_bounds_v1(now)
    try:
        res = requests.get(
            BINANCE_KLINES_URL,
            params={
                "symbol": "BTCUSDT",
                "interval": "5m",
                "startTime": int(window_start_ts * 1000),
                "limit": 1,
            },
            timeout=TIMEOUT,
        )
        res.raise_for_status()
        data = res.json() or []
        row = data[0] if data else None
        open_price = _safe_float(row[1]) if row and len(row) > 1 else None
        if open_price is None:
            return Window5mV1(window_start_ts, window_end_ts, None, False, "missing_open_price")
        return Window5mV1(window_start_ts, window_end_ts, open_price, True, "ok")
    except Exception as exc:
        return Window5mV1(window_start_ts, window_end_ts, None, False, f"{type(exc).__name__}: {exc}")


def seconds_to_end_v1(window_end_ts: float, now: Optional[float] = None) -> int:
    import time as _time

    now = _time.time() if now is None else now
    return max(0, int(window_end_ts - now))


__all__ = [
    "Window5mV1",
    "current_window_bounds_v1",
    "fetch_current_5m_window_v1",
    "seconds_to_end_v1",
    "fetch_external_btc_reference_v1",
    "WINDOW_SECONDS",
]
