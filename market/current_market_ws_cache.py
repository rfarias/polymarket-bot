from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Dict, Optional

import websockets

MARKET_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _top_levels(levels: Any) -> list[dict[str, Optional[float]]]:
    out: list[dict[str, Optional[float]]] = []
    for level in (levels or [])[:3]:
        out.append(
            {
                "price": _safe_float((level or {}).get("price")),
                "size": _safe_float((level or {}).get("size")),
            }
        )
    return out


class CurrentMarketWsCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._current_slug = ""
        self._desired_tokens: list[str] = []
        self._outcome_by_token: dict[str, str] = {}
        self._token_state: dict[str, dict[str, Any]] = {}
        self._worker: Optional[threading.Thread] = None
        self._updated_at: float = 0.0
        self._connected: bool = False
        self._last_error: str = ""

    def configure(self, slug: str, token_mapping: list[dict[str, Any]]) -> None:
        tokens = [str((entry or {}).get("token_id") or "") for entry in token_mapping if (entry or {}).get("token_id")]
        outcome_by_token = {
            str((entry or {}).get("token_id") or ""): str((entry or {}).get("outcome") or "")
            for entry in token_mapping
            if (entry or {}).get("token_id")
        }
        with self._lock:
            changed = slug != self._current_slug or tokens != self._desired_tokens
            self._current_slug = slug
            self._desired_tokens = tokens
            self._outcome_by_token = outcome_by_token
            if changed:
                self._token_state = {
                    token_id: {
                        "token_id": token_id,
                        "outcome": outcome_by_token.get(token_id),
                        "best_bid": None,
                        "best_ask": None,
                        "last_trade_price": None,
                        "tick_size": None,
                        "min_order_size": None,
                        "top_bids": [],
                        "top_asks": [],
                    }
                    for token_id in tokens
                }
                self._updated_at = 0.0
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run_forever, daemon=True)
            self._worker.start()

    def snapshot(self, max_age_secs: float = 2.0) -> Optional[dict[str, Any]]:
        with self._lock:
            updated_at = self._updated_at
            if not self._desired_tokens or updated_at <= 0:
                return None
            if time.time() - updated_at > max_age_secs:
                return None
            by_outcome: dict[str, dict[str, Any]] = {}
            for token_id, item in self._token_state.items():
                outcome = str(item.get("outcome") or "").lower()
                if outcome:
                    by_outcome[outcome] = dict(item)
            return {
                "slug": self._current_slug,
                "updated_at": updated_at,
                "connected": self._connected,
                "last_error": self._last_error,
                "up": by_outcome.get("up"),
                "down": by_outcome.get("down"),
            }

    def _run_forever(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        while True:
            tokens = self._desired_token_snapshot()
            if not tokens:
                await asyncio.sleep(0.25)
                continue
            try:
                async with websockets.connect(
                    MARKET_WSS,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    open_timeout=15,
                    max_queue=128,
                ) as ws:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market", "custom_feature_enabled": True}))
                    with self._lock:
                        self._connected = True
                        self._last_error = ""
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    try:
                        while True:
                            if tokens != self._desired_token_snapshot():
                                break
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                            if raw == "PONG":
                                continue
                            try:
                                payload = json.loads(raw)
                            except Exception:
                                continue
                            messages = payload if isinstance(payload, list) else [payload]
                            for msg in messages:
                                self._apply_message(msg)
                    finally:
                        heartbeat.cancel()
                        with self._lock:
                            self._connected = False
            except Exception as exc:
                with self._lock:
                    self._connected = False
                    self._last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.0)

    async def _heartbeat(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(8)
            try:
                await ws.send(json.dumps({}))
            except Exception:
                return

    def _desired_token_snapshot(self) -> list[str]:
        with self._lock:
            return list(self._desired_tokens)

    def _apply_message(self, msg: dict[str, Any]) -> None:
        event_type = msg.get("event_type")
        if event_type == "price_change":
            for change in msg.get("price_changes") or []:
                self._apply_single_price_change(change)
            return
        asset_id = str(msg.get("asset_id") or "")
        if not asset_id:
            return
        with self._lock:
            item = self._token_state.get(asset_id)
            if not item:
                return
            if event_type == "book":
                bids = _top_levels(msg.get("bids"))
                asks = _top_levels(msg.get("asks"))
                item["top_bids"] = bids
                item["top_asks"] = asks
                item["best_bid"] = bids[0]["price"] if bids else item.get("best_bid")
                item["best_ask"] = asks[0]["price"] if asks else item.get("best_ask")
                item["tick_size"] = _safe_float(msg.get("tick_size")) or item.get("tick_size")
                item["min_order_size"] = _safe_float(msg.get("min_order_size")) or item.get("min_order_size")
            elif event_type == "best_bid_ask":
                item["best_bid"] = _safe_float(msg.get("best_bid")) if msg.get("best_bid") is not None else item.get("best_bid")
                item["best_ask"] = _safe_float(msg.get("best_ask")) if msg.get("best_ask") is not None else item.get("best_ask")
                item["min_order_size"] = _safe_float(msg.get("min_order_size")) or item.get("min_order_size")
            elif event_type == "last_trade_price":
                item["last_trade_price"] = _safe_float(msg.get("price")) or item.get("last_trade_price")
            elif event_type == "tick_size_change":
                item["tick_size"] = _safe_float(msg.get("new_tick_size")) or item.get("tick_size")
            self._updated_at = time.time()

    def _apply_single_price_change(self, change: dict[str, Any]) -> None:
        asset_id = str(change.get("asset_id") or "")
        if not asset_id:
            return
        with self._lock:
            item = self._token_state.get(asset_id)
            if not item:
                return
            if change.get("best_bid") is not None:
                item["best_bid"] = _safe_float(change.get("best_bid"))
            if change.get("best_ask") is not None:
                item["best_ask"] = _safe_float(change.get("best_ask"))
            self._updated_at = time.time()
