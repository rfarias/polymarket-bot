import asyncio
import json
import time
from typing import Any, Dict, Optional

import websockets

from market.queue_5m_v3 import build_5m_queue_v3, UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1
from market.book_5m import fetch_market_metadata_from_slug
from market.setup1_policy import classify_signal, evaluate_entry_quality, plan_two_leg_order

MARKET_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PLACEHOLDER_BID_MAX = 0.02
PLACEHOLDER_ASK_MIN = 0.98
MIN_STABLE_SNAPSHOTS = 2
MIN_SHARES_PER_LEG = 5
DEFAULT_TICK_SIZE = 0.01

STATE_IDLE = "idle"
STATE_WATCHING = "watching"
STATE_ARMED = "armed"
STATE_EXIT_TRIGGERED = "exit_triggered"
STATE_SIM_PLANNED = "sim_planned"
STATE_SIM_EXPIRED = "sim_expired"
STATE_SKIPPED_TIMING = "skipped_timing"


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
            if asset_id in state:
                _apply_price_update(state[asset_id], change)
        return

    asset_id = str(msg.get("asset_id") or "")
    if asset_id not in state:
        return

    item = state[asset_id]
    if event_type == "book":
        item["best_bid"] = _extract_best_bid_from_book(msg)
        item["best_ask"] = _extract_best_ask_from_book(msg)
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
        "up_bid": up_bid,
        "up_ask": up_ask,
        "down_bid": down_bid,
        "down_ask": down_ask,
        "sum_asks": round(up_ask + down_ask, 4),
        "sum_bids": round(up_bid + down_bid, 4),
        "edge_asks": round(1 - (up_ask + down_ask), 4),
        "edge_bids": round((up_bid + down_bid) - 1, 4),
    }


def _transition_state(prev: str, signal: str, secs_to_end: Optional[int], exit_trigger: Optional[int]) -> str:
    if prev in (STATE_SIM_EXPIRED, STATE_SKIPPED_TIMING):
        return prev
    if secs_to_end is not None and exit_trigger is not None and secs_to_end <= exit_trigger:
        return STATE_EXIT_TRIGGERED
    if prev == STATE_SIM_PLANNED:
        return prev
    if signal == "armed":
        return STATE_ARMED
    if signal == "watching":
        return STATE_WATCHING
    return STATE_IDLE


def _maybe_fill_entries(sim_order: Dict[str, Any], metrics: Dict[str, float]) -> list[str]:
    events = []
    if not sim_order["up_filled"] and metrics["up_ask"] <= sim_order["up_limit_price"]:
        sim_order["up_filled"] = True
        sim_order["up_fill_price"] = metrics["up_ask"]
        events.append(f"up leg filled @ {metrics['up_ask']}")
    if not sim_order["down_filled"] and metrics["down_ask"] <= sim_order["down_limit_price"]:
        sim_order["down_filled"] = True
        sim_order["down_fill_price"] = metrics["down_ask"]
        events.append(f"down leg filled @ {metrics['down_ask']}")
    if sim_order["up_filled"] and sim_order["down_filled"] and sim_order["status"] == "planned":
        sim_order["status"] = "entries_filled"
        events.append("both entry legs filled")
    return events


def _maybe_post_exit(sim_order: Dict[str, Any]) -> Optional[str]:
    if sim_order["status"] != "entries_filled" or sim_order["exit_posted"]:
        return None
    sim_order["exit_posted"] = True
    sim_order["exit_up_price"] = round(float(sim_order["up_fill_price"]) + DEFAULT_TICK_SIZE, 2)
    sim_order["exit_down_price"] = round(float(sim_order["down_fill_price"]) + DEFAULT_TICK_SIZE, 2)
    sim_order["status"] = "exit_posted"
    return f"exit posted @ up={sim_order['exit_up_price']} down={sim_order['exit_down_price']}"


def _maybe_fill_exit(sim_order: Dict[str, Any], metrics: Dict[str, float]) -> list[str]:
    events = []
    if sim_order["status"] != "exit_posted":
        return events
    if not sim_order["exit_up_filled"] and metrics["up_bid"] >= sim_order["exit_up_price"]:
        sim_order["exit_up_filled"] = True
        events.append(f"up exit filled @ {metrics['up_bid']}")
    if not sim_order["exit_down_filled"] and metrics["down_bid"] >= sim_order["exit_down_price"]:
        sim_order["exit_down_filled"] = True
        events.append(f"down exit filled @ {metrics['down_bid']}")
    if sim_order["exit_up_filled"] and sim_order["exit_down_filled"]:
        sim_order["status"] = "completed"
        gross = round((sim_order['exit_up_price'] - sim_order['up_fill_price']) + (sim_order['exit_down_price'] - sim_order['down_fill_price']), 4)
        events.append(f"simulation completed | gross_move={gross}")
    return events


def _force_close_remaining(sim_order: Dict[str, Any], metrics: Dict[str, float], reason: str) -> list[str]:
    events = []
    if sim_order["status"] in ("completed", "force_closed", "expired_unfilled"):
        return events
    if sim_order["up_filled"] and not sim_order["exit_up_filled"]:
        sim_order["exit_up_filled"] = True
        sim_order["force_close_up_price"] = metrics["up_bid"]
        events.append(f"up force-closed @ {metrics['up_bid']} ({reason})")
    if sim_order["down_filled"] and not sim_order["exit_down_filled"]:
        sim_order["exit_down_filled"] = True
        sim_order["force_close_down_price"] = metrics["down_bid"]
        events.append(f"down force-closed @ {metrics['down_bid']} ({reason})")
    if not sim_order["up_filled"] and not sim_order["down_filled"]:
        sim_order["status"] = "expired_unfilled"
        events.append(f"no filled entries to close ({reason})")
        return events
    if sim_order["exit_up_filled"] and sim_order["exit_down_filled"]:
        up_exit = sim_order["force_close_up_price"] if sim_order["force_close_up_price"] is not None else sim_order["exit_up_price"]
        down_exit = sim_order["force_close_down_price"] if sim_order["force_close_down_price"] is not None else sim_order["exit_down_price"]
        gross = round((float(up_exit) - float(sim_order['up_fill_price'])) + (float(down_exit) - float(sim_order['down_fill_price'])), 4)
        sim_order["status"] = "force_closed"
        events.append(f"simulation force-closed | gross_move={gross}")
    return events


async def _heartbeat(ws):
    while True:
        await asyncio.sleep(8)
        try:
            await ws.send(json.dumps({}))
        except Exception:
            return


async def simulate_5m_execution_v4(duration_seconds: int = 40) -> None:
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

    stable_counts = {"current": 0, "next_1": 0, "next_2": 0}
    states = {"current": STATE_IDLE, "next_1": STATE_IDLE, "next_2": STATE_IDLE}
    sim_orders = {"next_1": None, "next_2": None}
    focus_slot = "next_1"
    focus_announced = False
    last_filter_reason = {"next_1": None, "next_2": None}

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
                    print("\n===== 5M EXECUTION SIM SNAPSHOT V4 =====")
                    trigger = slot_bundle["unfilled_exit_trigger_secs_to_end_on_next_1"]
                    for slot_name in ("current", "next_1", "next_2"):
                        item = slot_bundle["queue"].get(slot_name)
                        secs = _current_secs_to_end(item.get("seconds_to_end") if item else None, started_at)
                        snap = _slot_snapshot(registry, slot_name)
                        metrics = _compute_metrics(snap)
                        if metrics:
                            stable_counts[slot_name] += 1
                        else:
                            stable_counts[slot_name] = 0

                        signal = classify_signal(metrics, stable_counts[slot_name], MIN_STABLE_SNAPSHOTS)
                        exit_trigger = trigger if slot_name == "next_1" else None
                        new_state = _transition_state(states[slot_name], signal, secs, exit_trigger)

                        if slot_name == "next_1" and new_state == STATE_EXIT_TRIGGERED and sim_orders[slot_name] is None and not focus_announced:
                            print(f"[SKIP] next_1 skipped due to timing: secs_to_end={secs} <= {trigger}")
                            print("[FOCUS] execution focus moved to next_2")
                            states[slot_name] = STATE_SKIPPED_TIMING
                            focus_slot = "next_2"
                            focus_announced = True
                            new_state = STATE_SKIPPED_TIMING
                        elif slot_name in sim_orders and new_state == STATE_EXIT_TRIGGERED and sim_orders[slot_name] is not None:
                            if metrics:
                                print(f"[SIM] {slot_name} exit trigger reached -> force-closing remaining exposure")
                                for event in _force_close_remaining(sim_orders[slot_name], metrics, "time_trigger"):
                                    print(f"[FORCE_CLOSE] {slot_name}: {event}")
                            else:
                                sim_orders[slot_name]["status"] = "expired_no_metrics"
                            states[slot_name] = STATE_SIM_EXPIRED
                            new_state = STATE_SIM_EXPIRED
                            if slot_name == "next_1" and focus_slot != "next_2":
                                print("[ROLL] focus rolled from next_1 to next_2")
                                focus_slot = "next_2"
                                focus_announced = True
                        elif slot_name in sim_orders and new_state in (STATE_WATCHING, STATE_ARMED) and sim_orders[slot_name] is None and metrics:
                            ok, reason, details = evaluate_entry_quality(metrics, slot_name, secs)
                            if ok:
                                sim_orders[slot_name] = plan_two_leg_order(metrics, MIN_SHARES_PER_LEG)
                                sim_orders[slot_name].update({
                                    "up_filled": False,
                                    "down_filled": False,
                                    "up_fill_price": None,
                                    "down_fill_price": None,
                                    "exit_posted": False,
                                    "exit_up_price": None,
                                    "exit_down_price": None,
                                    "exit_up_filled": False,
                                    "exit_down_filled": False,
                                    "force_close_up_price": None,
                                    "force_close_down_price": None,
                                    "status": "planned",
                                })
                                states[slot_name] = STATE_SIM_PLANNED
                                new_state = STATE_SIM_PLANNED
                                last_filter_reason[slot_name] = None
                                print(f"[SIM] {slot_name} planned -> up={metrics['up_ask']} down={metrics['down_ask']} sum_asks={metrics['sum_asks']} qty={MIN_SHARES_PER_LEG}x{MIN_SHARES_PER_LEG}")
                                print(f"[QUALITY] {slot_name}: {details}")
                            else:
                                if last_filter_reason[slot_name] != reason:
                                    print(f"[FILTER] {slot_name}: entry blocked -> {reason}")
                                    if details:
                                        print(f"[QUALITY] {slot_name}: {details}")
                                    last_filter_reason[slot_name] = reason
                        elif new_state != states[slot_name]:
                            print(f"[STATE] {slot_name}: {states[slot_name]} -> {new_state}")
                            states[slot_name] = new_state

                        if slot_name in sim_orders and sim_orders[slot_name] is not None and metrics and states[slot_name] in (STATE_SIM_PLANNED, STATE_ARMED, STATE_WATCHING):
                            for event in _maybe_fill_entries(sim_orders[slot_name], metrics):
                                print(f"[FILL] {slot_name}: {event}")
                            exit_event = _maybe_post_exit(sim_orders[slot_name])
                            if exit_event:
                                print(f"[EXIT] {slot_name}: {exit_event}")
                            for event in _maybe_fill_exit(sim_orders[slot_name], metrics):
                                print(f"[EXIT_FILL] {slot_name}: {event}")

                        marker = " <FOCUS>" if slot_name == focus_slot else ""
                        print(f"\n[{slot_name.upper()}]{marker} secs_to_end={secs} | stable_count={stable_counts[slot_name]} | signal={signal} | state={states[slot_name]}")
                        if metrics:
                            print(f"UP/DOWN asks={metrics['up_ask']}/{metrics['down_ask']} | bids={metrics['up_bid']}/{metrics['down_bid']}")
                            print(f"SUM_ASKS={metrics['sum_asks']} | edge_asks={metrics['edge_asks']}")
                            print(f"SUM_BIDS={metrics['sum_bids']} | edge_bids={metrics['edge_bids']}")
                        else:
                            print("metrics=placeholder_or_incomplete")
                        if slot_name in sim_orders and sim_orders[slot_name] is not None:
                            print(f"[SIM_ORDER] {slot_name} -> {sim_orders[slot_name]}")
        finally:
            hb.cancel()
