import asyncio
import json
import time
from typing import Any, Dict, Optional

import websockets

from market.queue_5m_v3 import build_5m_queue_v3, UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1
from market.book_5m import fetch_market_metadata_from_slug

MARKET_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PLACEHOLDER_BID_MAX = 0.02
PLACEHOLDER_ASK_MIN = 0.98
ARBITRAGE_SUM_ASKS_MAX = 0.99
WATCH_SUM_ASKS_MAX = 1.02


def _build_slot_bundle() -> Dict[str, Any]:
    queue = build_5m_queue_v3()
    slots: Dict[str, Any] = {}

    for slot_name in ("current", "next_1", "next_2"):
        item = queue.get(slot_name)
        if not item:
            slots[slot_name] = None
            continue

        meta = fetch_market_metadata_from_slug(item["slug"])
        if not meta:
            slots[slot_name] = None
            continue

        slots[slot_name] = {"item": item, "meta": meta}

    return {
        "queue": queue,
        "slots": slots,
        "unfilled_exit_trigger_secs_to_end_on_next_1": UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1,
    }


def _build_registry(slot_bundle: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {}
    for slot_name, slot in slot_bundle["slots"].items():
        if not slot:
            continue
        item = slot["item"]
        meta = slot["meta"]
        for entry in meta.get("token_mapping") or []:
            token_id = str(entry.get("token_id"))
            if not token_id:
                continue
            registry[token_id] = {
                "slot_name": slot_name,
                "event_slug": item.get("slug"),
                "event_title": item.get("title"),
                "seconds_to_end_start": item.get("seconds_to_end"),
                "outcome": entry.get("outcome"),
                "token_id": token_id,
                "best_bid": None,
                "best_ask": None,
                "last_trade_price": None,
                "tick_size": None,
            }
    return registry


def _extract_best_bid_from_book(msg: Dict[str, Any]) -> Optional[float]:
    bids = msg.get("bids") or []
    if not bids:
        return None
    try:
        return float(bids[0]["price"])
    except Exception:
        return None


def _extract_best_ask_from_book(msg: Dict[str, Any]) -> Optional[float]:
    asks = msg.get("asks") or []
    if not asks:
        return None
    try:
        return float(asks[0]["price"])
    except Exception:
        return None


def _apply_price_update(item: Dict[str, Any], change: Dict[str, Any]) -> None:
    try:
        if change.get("best_bid") is not None:
            item["best_bid"] = float(change.get("best_bid"))
    except Exception:
        pass
    try:
        if change.get("best_ask") is not None:
            item["best_ask"] = float(change.get("best_ask"))
    except Exception:
        pass


def _update_state(state: Dict[str, Dict[str, Any]], msg: Dict[str, Any]) -> None:
    event_type = msg.get("event_type")

    if event_type == "price_change":
        for change in msg.get("price_changes") or []:
            asset_id = str(change.get("asset_id") or "")
            if asset_id not in state:
                continue
            _apply_price_update(state[asset_id], change)
        return

    asset_id = str(msg.get("asset_id") or "")
    if asset_id not in state:
        return

    item = state[asset_id]
    if event_type == "book":
        item["best_bid"] = _extract_best_bid_from_book(msg)
        item["best_ask"] = _extract_best_ask_from_book(msg)
        item["tick_size"] = msg.get("tick_size") or item.get("tick_size")
    elif event_type == "best_bid_ask":
        try:
            if msg.get("best_bid") is not None:
                item["best_bid"] = float(msg.get("best_bid"))
        except Exception:
            pass
        try:
            if msg.get("best_ask") is not None:
                item["best_ask"] = float(msg.get("best_ask"))
        except Exception:
            pass
    elif event_type == "last_trade_price":
        try:
            item["last_trade_price"] = float(msg.get("price"))
        except Exception:
            pass
    elif event_type == "tick_size_change":
        item["tick_size"] = msg.get("new_tick_size") or item.get("tick_size")


def _slot_snapshot(state: Dict[str, Dict[str, Any]], slot_name: str) -> Dict[str, Any]:
    up = None
    down = None
    for item in state.values():
        if item["slot_name"] != slot_name:
            continue
        outcome = str(item.get("outcome") or "").lower()
        if outcome == "up":
            up = item
        elif outcome == "down":
            down = item
    return {"up": up, "down": down}


def _current_secs_to_end(start_secs: Optional[int], started_at: float) -> Optional[int]:
    if start_secs is None:
        return None
    elapsed = time.time() - started_at
    return max(0, int(round(start_secs - elapsed)))


def _is_placeholder_pair(up: Dict[str, Any], down: Dict[str, Any]) -> bool:
    vals = [up.get("best_bid"), up.get("best_ask"), down.get("best_bid"), down.get("best_ask")]
    if any(v is None for v in vals):
        return True
    return float(up["best_bid"]) <= PLACEHOLDER_BID_MAX and float(down["best_bid"]) <= PLACEHOLDER_BID_MAX and float(up["best_ask"]) >= PLACEHOLDER_ASK_MIN and float(down["best_ask"]) >= PLACEHOLDER_ASK_MIN


def _compute_metrics(snap: Dict[str, Any]) -> Optional[Dict[str, float]]:
    up = snap.get("up")
    down = snap.get("down")
    if not up or not down:
        return None
    if _is_placeholder_pair(up, down):
        return None
    up_bid = float(up["best_bid"])
    up_ask = float(up["best_ask"])
    down_bid = float(down["best_bid"])
    down_ask = float(down["best_ask"])
    return {
        "sum_asks": round(up_ask + down_ask, 4),
        "sum_bids": round(up_bid + down_bid, 4),
    }


def _print_slot(slot_name: str, snap: Dict[str, Any], secs_to_end: Optional[int], trigger: int) -> None:
    up = snap.get("up")
    down = snap.get("down")
    print(f"\n[{slot_name.upper()}]")
    print(f"secs_to_end={secs_to_end}")
    if up:
        print(f"UP   -> bid={up.get('best_bid')} ask={up.get('best_ask')} ltp={up.get('last_trade_price')}")
    else:
        print("UP   -> none")
    if down:
        print(f"DOWN -> bid={down.get('best_bid')} ask={down.get('best_ask')} ltp={down.get('last_trade_price')}")
    else:
        print("DOWN -> none")

    metrics = _compute_metrics(snap)
    if not metrics:
        print("state=placeholder_or_incomplete")
    else:
        print(f"SUM_ASKS={metrics['sum_asks']}")
        print(f"SUM_BIDS={metrics['sum_bids']}")
        if slot_name in ("next_1", "next_2"):
            if metrics["sum_asks"] <= ARBITRAGE_SUM_ASKS_MAX:
                print(f"[ALERT] arbitrage_candidate: sum_asks <= {ARBITRAGE_SUM_ASKS_MAX}")
            elif metrics["sum_asks"] <= WATCH_SUM_ASKS_MAX:
                print(f"[WATCH] near_arb_zone: sum_asks <= {WATCH_SUM_ASKS_MAX}")

    if slot_name == "next_1" and secs_to_end is not None and secs_to_end <= trigger:
        print(f"[RULE] next_1 reached exit trigger: secs_to_end <= {trigger}")


async def _heartbeat(ws):
    while True:
        await asyncio.sleep(8)
        try:
            await ws.send(json.dumps({}))
        except Exception:
            return


async def monitor_5m_signals_v2(duration_seconds: int = 20) -> None:
    slot_bundle = _build_slot_bundle()
    registry = _build_registry(slot_bundle)
    asset_ids = list(registry.keys())

    print("[QUEUE] 5m queue summary:")
    for slot_name in ("current", "next_1", "next_2"):
        item = slot_bundle["queue"].get(slot_name)
        if item:
            print(f"- {slot_name}: {item['seconds_to_end']}s | {item['title']} | slug={item['slug']}")
        else:
            print(f"- {slot_name}: none")

    if not asset_ids:
        print("[WSS] No asset_ids available for 5m queue")
        return

    async with websockets.connect(MARKET_WSS, ping_interval=None, close_timeout=5) as ws:
        await ws.send(json.dumps({"assets_ids": asset_ids, "type": "market", "custom_feature_enabled": True}))
        print(f"[WSS] Subscribed to {len(asset_ids)} asset_ids")

        hb = asyncio.create_task(_heartbeat(ws))
        started_at = time.time()
        last_print = 0.0

        try:
            while time.time() - started_at < duration_seconds:
                timeout_left = max(0.1, duration_seconds - (time.time() - started_at))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(5, timeout_left))
                except TimeoutError:
                    raw = None

                if raw:
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        payload = None
                    if payload is not None:
                        messages = payload if isinstance(payload, list) else [payload]
                        for msg in messages:
                            _update_state(registry, msg)

                if time.time() - last_print >= 2:
                    last_print = time.time()
                    print("\n===== LIVE 5M SIGNAL SNAPSHOT =====")
                    trigger = slot_bundle["unfilled_exit_trigger_secs_to_end_on_next_1"]
                    for slot_name in ("current", "next_1", "next_2"):
                        item = slot_bundle["queue"].get(slot_name)
                        secs = _current_secs_to_end(item.get("seconds_to_end") if item else None, started_at)
                        snap = _slot_snapshot(registry, slot_name)
                        _print_slot(slot_name, snap, secs, trigger)
        finally:
            hb.cancel()
