"""
market/binance_ws_v1.py

Binance WebSocket tick collector for real-time BTC price.
Runs in a background thread; main thread reads via thread-safe accessors.

Usage:
    ws = BinanceTickFeed()
    ws.start()                      # starts background thread
    price = ws.current_price()      # latest trade price
    ticks = ws.ticks_last_n_secs(8) # list of (ts, price) within N seconds
    score = ws.tick_direction_score(8)  # -1..+1 directional consistency
    ws.stop()
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

import websockets

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
_TICK_BUFFER_SECS = 120.0  # keep 2 min of history


class BinanceTickFeed:
    """Thread-safe Binance WebSocket tick collector."""

    def __init__(self, symbol: str = "btcusdt"):
        self._symbol = symbol
        self._url = f"wss://stream.binance.com:9443/ws/{symbol}@trade"
        self._ticks: Deque[Tuple[float, float]] = deque()  # (unix_ts, price)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._last_price: float = 0.0
        self._connect_time: float = 0.0

    # ------------------------------------------------------------------
    # Public accessors (thread-safe)
    # ------------------------------------------------------------------

    def current_price(self) -> float:
        with self._lock:
            return self._last_price

    def ticks_last_n_secs(self, n: float) -> List[Tuple[float, float]]:
        """Returns list of (ts, price) from the last N seconds."""
        cutoff = time.time() - n
        with self._lock:
            return [(ts, p) for ts, p in self._ticks if ts >= cutoff]

    def tick_direction_score(self, n_secs: float = 8.0) -> float:
        """
        Returns directional consistency in [-1, +1]:
          +1.0 = all ticks moving up
          -1.0 = all ticks moving down
           0.0 = flat or mixed
        Based on fraction of up-ticks vs down-ticks over the last N seconds.
        """
        ticks = self.ticks_last_n_secs(n_secs)
        if len(ticks) < 2:
            return 0.0
        ups = 0
        downs = 0
        for i in range(1, len(ticks)):
            delta = ticks[i][1] - ticks[i - 1][1]
            if delta > 0:
                ups += 1
            elif delta < 0:
                downs += 1
        total = ups + downs
        if total == 0:
            return 0.0
        return (ups - downs) / total

    def is_connected(self) -> bool:
        return self._running and self._last_price > 0

    def wait_for_first_tick(self, timeout: float = 10.0) -> bool:
        """Block until first tick received or timeout. Returns True if connected."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.current_price() > 0:
                return True
            time.sleep(0.1)
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="BinanceWS")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        while self._running:
            try:
                self._loop.run_until_complete(self._listen())
            except Exception as exc:
                print(f"[BinanceWS] Disconnected: {exc!r} — reconnecting in 3s")
                time.sleep(3)

    async def _listen(self) -> None:
        async with websockets.connect(
            self._url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._connect_time = time.time()
            print(f"[BinanceWS] Connected: {self._url}")
            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    ts = float(msg["T"]) / 1000.0  # trade time in ms → seconds
                    price = float(msg["p"])
                    self._ingest(ts, price)
                except Exception:
                    pass

    def _ingest(self, ts: float, price: float) -> None:
        cutoff = ts - _TICK_BUFFER_SECS
        with self._lock:
            self._last_price = price
            self._ticks.append((ts, price))
            # evict old ticks
            while self._ticks and self._ticks[0][0] < cutoff:
                self._ticks.popleft()


# ------------------------------------------------------------------
# Binance REST helpers (no WebSocket dependency)
# ------------------------------------------------------------------

import urllib.request


BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def fetch_binance_price_at(window_ts: int, symbol: str = "BTCUSDT") -> Optional[float]:
    """
    Fetch the Binance 1-minute candle open price at the exact window_ts.
    window_ts must be a Unix timestamp divisible by 60.
    Returns None on error.
    """
    start_ms = window_ts * 1000
    end_ms = start_ms + 59_999
    url = (
        f"{BINANCE_KLINES_URL}?symbol={symbol}&interval=1m"
        f"&startTime={start_ms}&endTime={end_ms}&limit=1"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            rows = json.loads(resp.read())
        if rows:
            return float(rows[0][1])  # open price
    except Exception as exc:
        print(f"[Binance REST] fetch_binance_price_at error: {exc}")
    return None


def fetch_binance_klines_recent(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    limit: int = 30,
) -> List[dict]:
    """
    Fetch recent closed klines. Returns list of dicts:
    {open_time, open, high, low, close, volume}
    Most recent closed candle is last.
    """
    url = f"{BINANCE_KLINES_URL}?symbol={symbol}&interval={interval}&limit={limit + 1}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            rows = json.loads(resp.read())
        candles = [
            {
                "open_time": int(r[0]) // 1000,
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
            }
            for r in rows[:-1]  # drop the currently-open candle
        ]
        return candles
    except Exception as exc:
        print(f"[Binance REST] fetch_binance_klines_recent error: {exc}")
        return []
