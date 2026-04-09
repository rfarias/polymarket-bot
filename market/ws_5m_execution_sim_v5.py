import asyncio
import json
import time
from typing import Any, Dict, Optional

import websockets

from market.queue_5m_v3 import build_5m_queue_v3, UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1
from market.book_5m import fetch_market_metadata_from_slug
from market.setup1_policy import classify_signal, evaluate_entry_quality, plan_two_leg_order
from market.setup1_order_manager_v2 import Setup1OrderManagerV2, PLAN_DONE, PLAN_FORCE_CLOSED, PLAN_HEDGED, PLAN_EXIT_POSTED

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
STATE_PLAN_WORKING = "plan_working"
STATE_PLAN_DONE = "plan_done"
STATE_PLAN_FORCE_CLOSED = "plan_force_closed"
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
    if prev in (STATE_PLAN_DONE, STATE_PLAN_FORCE_CLOSED, STATE_SKIPPED_TIMING):
        return prev
    if secs_to_end is not None and exit_trigger is not None and secs_to_end <= exit_trigger:
        return STATE_EXIT_TRIGGERED
    if prev == STATE_PLAN_WORKING:
        return prev
    if signal == "armed":
        return STATE_ARMED
    if signal == "watching":
        return STATE_WATCHING
    return STATE_IDLE


def _entry_fill_qty(limit_price: float, ask_price: float, remaining_qty: int) -> int:
    if ask_price < limit_price:
        return min(2, remaining_qty)
    if ask_price == limit_price:
        return min(1, remaining_qty)
    return 0


def _exit_fill_qty(limit_price: float, bid_price: float, remaining_qty: int) -> int:
    if bid_price > limit_price:
        return min(2, remaining_qty)
    if bid_price == limit_price:
        return min(1, remaining_qty)
    return 0


async def _heartbeat(ws):
    while True:
        await asyncio.sleep(8)
        try:
            await ws.send(json.dumps({}))
        except Exception:
            return


async def simulate_5m_execution_v5(duration_seconds: int = 40) -> None:
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
    focus_slot = "next_1"
    focus_announced = False
    last_filter_reason = {"next_1": None, "next_2": None}
    slot_plan_ids: Dict[str, str] = {}
    manager = Setup1OrderManagerV2()

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
                    print("\n===== 5M EXECUTION SIM SNAPSHOT V5 =====")
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
                        plan_id = slot_plan_ids.get(slot_name)
                        plan = manager.get_plan(plan_id) if plan_id else None

                        if slot_name == "next_1" and new_state == STATE_EXIT_TRIGGERED and plan is None and not focus_announced:
                            print(f"[SKIP] next_1 skipped due to timing: secs_to_end={secs} <= {trigger}")
                            print("[FOCUS] execution focus moved to next_2")
                            states[slot_name] = STATE_SKIPPED_TIMING
                            focus_slot = "next_2"
                            focus_announced = True
                            new_state = STATE_SKIPPED_TIMING
                        elif slot_name in slot_plan_ids and new_state == STATE_EXIT_TRIGGERED and plan is not None:
                            print(f"[DEADLINE] {slot_name}: deadline reached for active plan {plan.plan_id}")
                            for event in manager.on_deadline(plan.plan_id):
                                print(f"[PLAN] {slot_name}: {event}")
                            if metrics:
                                for event in manager.force_close_plan(plan.plan_id, "time_trigger", metrics['up_bid'], metrics['down_bid']):
                                    print(f"[FORCE_CLOSE] {slot_name}: {event}")
                            states[slot_name] = STATE_PLAN_FORCE_CLOSED
                            new_state = STATE_PLAN_FORCE_CLOSED
                            if slot_name == "next_1" and focus_slot != "next_2":
                                print("[ROLL] focus rolled from next_1 to next_2")
                                focus_slot = "next_2"
                                focus_announced = True
                        elif slot_name in ("next_1", "next_2") and plan is None and new_state in (STATE_WATCHING, STATE_ARMED) and metrics:
                            ok, reason, details = evaluate_entry_quality(metrics, slot_name, secs)
                            if ok:
                                two_leg = plan_two_leg_order(metrics, MIN_SHARES_PER_LEG)
                                created_plan, events = manager.create_two_leg_plan(
                                    event_slug=item['slug'],
                                    slot_name=slot_name,
                                    up_price=two_leg['up_limit_price'],
                                    down_price=two_leg['down_limit_price'],
                                    qty_per_leg=MIN_SHARES_PER_LEG,
                                )
                                for event in events:
                                    print(f"[PLAN] {slot_name}: {event}")
                                if created_plan:
                                    slot_plan_ids[slot_name] = created_plan.plan_id
                                    plan = created_plan
                                    states[slot_name] = STATE_PLAN_WORKING
                                    new_state = STATE_PLAN_WORKING
                                    last_filter_reason[slot_name] = None
                                    print(f"[QUALITY] {slot_name}: {details}")
                            else:
                                if last_filter_reason[slot_name] != reason:
                                    print(f"[FILTER] {slot_name}: entry blocked -> {reason}")
                                    if details:
                                        print(f"[QUALITY] {slot_name}: {details}")
                                    last_filter_reason[slot_name] = reason
                        elif new_state != states[slot_name] and states[slot_name] not in (STATE_PLAN_WORKING, STATE_PLAN_DONE, STATE_PLAN_FORCE_CLOSED):
                            print(f"[STATE] {slot_name}: {states[slot_name]} -> {new_state}")
                            states[slot_name] = new_state

                        # per-plan fill simulation
                        plan = manager.get_plan(slot_plan_ids[slot_name]) if slot_name in slot_plan_ids else None
                        if plan and metrics and states[slot_name] not in (STATE_PLAN_DONE, STATE_PLAN_FORCE_CLOSED):
                            up_entry = plan.tickets['up_entry']
                            down_entry = plan.tickets['down_entry']
                            up_fill_qty = _entry_fill_qty(up_entry.price, metrics['up_ask'], up_entry.remaining_qty)
                            if up_fill_qty:
                                for event in manager.apply_fill(plan.plan_id, 'up_entry', up_fill_qty, metrics['up_ask']):
                                    print(f"[FILL] {slot_name}: {event}")
                            down_fill_qty = _entry_fill_qty(down_entry.price, metrics['down_ask'], down_entry.remaining_qty)
                            if down_fill_qty:
                                for event in manager.apply_fill(plan.plan_id, 'down_entry', down_fill_qty, metrics['down_ask']):
                                    print(f"[FILL] {slot_name}: {event}")

                            if plan.state == PLAN_HEDGED and 'up_exit' not in plan.tickets:
                                up_exit_price = round(plan.tickets['up_entry'].price + DEFAULT_TICK_SIZE, 2)
                                down_exit_price = round(plan.tickets['down_entry'].price + DEFAULT_TICK_SIZE, 2)
                                for event in manager.post_exit_orders(plan.plan_id, up_exit_price, down_exit_price):
                                    print(f"[EXIT] {slot_name}: {event}")

                            if plan.state == PLAN_EXIT_POSTED:
                                up_exit = plan.tickets['up_exit']
                                down_exit = plan.tickets['down_exit']
                                up_exit_qty = _exit_fill_qty(up_exit.price, metrics['up_bid'], up_exit.remaining_qty)
                                if up_exit_qty:
                                    for event in manager.apply_fill(plan.plan_id, 'up_exit', up_exit_qty, metrics['up_bid']):
                                        print(f"[EXIT_FILL] {slot_name}: {event}")
                                down_exit_qty = _exit_fill_qty(down_exit.price, metrics['down_bid'], down_exit.remaining_qty)
                                if down_exit_qty:
                                    for event in manager.apply_fill(plan.plan_id, 'down_exit', down_exit_qty, metrics['down_bid']):
                                        print(f"[EXIT_FILL] {slot_name}: {event}")

                            if plan.state == PLAN_DONE:
                                states[slot_name] = STATE_PLAN_DONE
                                print(f"[DONE] {slot_name}: plan completed")
                            elif plan.state == PLAN_FORCE_CLOSED:
                                states[slot_name] = STATE_PLAN_FORCE_CLOSED

                        marker = " <FOCUS>" if slot_name == focus_slot else ""
                        print(f"\n[{slot_name.upper()}]{marker} secs_to_end={secs} | stable_count={stable_counts[slot_name]} | signal={signal} | state={states[slot_name]}")
                        if metrics:
                            print(f"UP/DOWN asks={metrics['up_ask']}/{metrics['down_ask']} | bids={metrics['up_bid']}/{metrics['down_bid']}")
                            print(f"SUM_ASKS={metrics['sum_asks']} | edge_asks={metrics['edge_asks']}")
                            print(f"SUM_BIDS={metrics['sum_bids']} | edge_bids={metrics['edge_bids']}")
                        else:
                            print("metrics=placeholder_or_incomplete")
                        if plan:
                            print(f"[PLAN_STATE] {slot_name}: {plan.state}")
                            print(f"[PLAN_DETAIL] {slot_name}: {plan.as_dict()}")
        finally:
            hb.cancel()
