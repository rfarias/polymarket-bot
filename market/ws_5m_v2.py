import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import websockets

from market.queue_5m_v2 import build_5m_queue_v2, UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1
from market.book_5m import fetch_market_metadata_from_slug

MARKET_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _build_slot_bundle() -> Dict[str, Any]:
    queue = build_5m_queue_v2()
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

        slots[slot_name] = {
            "item": item,
            "meta": meta,
        }

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
        mappings = meta.get("token_mapping") or []

        for entry in mappings:
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
                "min_order_size": None,
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
        item["min_order_size"] = msg.get("min_order_size") or item.get("min_order_size")
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
        item["min_order_size"] = msg.get("min_order_size") or item.get("min_order_size")
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


def _print_slot(slot_name: str, snap: Dict[str, Any], secs_to_end: Optional[int], trigger: int) -> None:
    up = snap.get("up")
    down = snap.get("down")

    print(f"\n[{slot_name.upper()}]")
    print(f"secs_to_end={secs_to_end}")

    if up:
        print(f"UP   -> bid={up.get('best_bid')} ask={up.get('best_ask')} ltp={up.get('last_trade_price')} tick={up.get('tick_size')} min_size={up.get('min_order_size')}")
    else:
        print("UP   -> none")

    if down:
        print(f"DOWN -> bid={down.get('best_bid')} ask={down.get('best_ask')} ltp={down.get('last_trade_price')} tick={down.get('tick_size')} min_size={down.get('min_order_size')}")
    else:
        print("DOWN -> none")

    if up and down:
        up_ask = up.get("best_ask")
        down_ask = down.get("best_ask")
        up_bid = up.get("best_bid")
        down_bid = down.get("best_bid")
        if up_ask is not None and down_ask is not None:
            print(f"SUM_ASKS={round(float(up_ask) + float(down_ask), 4)}")
        if up_bid is not None and down_bid is not None:
            print(f"SUM_BIDS={round(float(up_bid) + float(down_bid), 4)}")

    if slot_name == "next_1" and secs_to_end is not None and secs_to_end <= trigger:
        print(f"[RULE] next_1 reached exit trigger: secs_to_end <= {trigger}")


async def _heartbeat(ws):
    while True:
        await asyncio.sleep(8)
        try:
            await ws.send(json.dumps({}))
        except Exception:
            return


async def monitor_5m_queue_ws_v2(duration_seconds: int = 20) -> None:
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
        sub = {
            "assets_ids": asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        await ws.send(json.dumps(sub))
        print(f"[WSS] Subscribed to {len(asset_ids)} asset_ids")

        hb = asyncio.create_task(_heartbeat(ws))
        started_at = time.time()
        last_print = 0.0

        try:
            while time.time() - started_at < duration_seconds:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue

                messages = payload if isinstance(payload, list) else [payload]
                for msg in messages:
                    _update_state(registry, msg)

                if time.time() - last_print >= 2:
                    last_print = time.time()
                    print("\n===== LIVE 5M SNAPSHOT =====")
                    trigger = slot_bundle["unfilled_exit_trigger_secs_to_end_on_next_1"]
                    for slot_name in ("current", "next_1", "next_2"):
                        item = slot_bundle["queue"].get(slot_name)
                        secs = _current_secs_to_end(item.get("seconds_to_end") if item else None, started_at)
                        snap = _slot_snapshot(registry, slot_name)
                        _print_slot(slot_name, snap, secs, trigger)
        finally:
            hb.cancel()
