import time
from typing import Any, Dict, Optional, Tuple

from market.book_5m import fetch_books_for_tokens, fetch_market_metadata_from_slug
from market.queue_5m_v5 import build_5m_queue_v5, UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1
from market.setup1_policy import ARBITRAGE_SUM_ASKS_MAX, classify_signal
from market.setup1_broker_executor_v3 import Setup1BrokerExecutorV3
from market.dryrun_broker import DryRunBroker
from market.public_market_data_v1 import fetch_midpoints, fetch_spread
from market.public_market_data_v2 import fetch_token_executable_prices

MIN_STABLE_SNAPSHOTS = 2
POLL_INTERVAL_SECONDS = 2.0
DISPLAY_SPREAD_WIDE_THRESHOLD = 0.10


def _build_slot_bundle() -> Dict[str, Any]:
    queue = build_5m_queue_v5()
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


def _best_bid(book: Dict[str, Any]) -> Optional[float]:
    bids = book.get("bids") or []
    if not bids:
        return None
    try:
        return float(bids[0]["price"])
    except Exception:
        return None


def _best_ask(book: Dict[str, Any]) -> Optional[float]:
    asks = book.get("asks") or []
    if not asks:
        return None
    try:
        return float(asks[0]["price"])
    except Exception:
        return None


def _raw_book_id(book: Dict[str, Any]) -> str:
    return str(book.get("asset_id") or book.get("token_id") or book.get("id") or "")


def _display_price(midpoint: Optional[float], spread: Optional[float], last_trade_price: Optional[float]) -> Tuple[Optional[float], str]:
    if midpoint is not None and spread is not None and spread <= DISPLAY_SPREAD_WIDE_THRESHOLD:
        return midpoint, "midpoint"
    if last_trade_price is not None:
        try:
            return float(last_trade_price), "last_trade_price"
        except Exception:
            pass
    if midpoint is not None:
        return midpoint, "midpoint_fallback"
    return None, "none"


def _fetch_slot_state(slot_bundle: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for slot_name, slot in slot_bundle["slots"].items():
        if not slot:
            result[slot_name] = None
            continue

        meta = slot["meta"]
        token_mapping = meta.get("token_mapping") or []
        token_ids = [str(x["token_id"]) for x in token_mapping if x.get("token_id")]
        raw_books = fetch_books_for_tokens(token_ids)
        by_id = {_raw_book_id(b): b for b in raw_books}
        midpoints = fetch_midpoints(token_ids)

        joined = []
        for mapping in token_mapping:
            token_id = str(mapping["token_id"])
            book = by_id.get(token_id) or {}
            best_bid = _best_bid(book)
            best_ask = _best_ask(book)
            midpoint = midpoints.get(token_id)
            spread = fetch_spread(token_id)
            executable = fetch_token_executable_prices(token_id)
            display_price, display_source = _display_price(midpoint, spread, book.get("last_trade_price"))
            top_bids = []
            top_asks = []
            for lvl in (book.get("bids") or [])[:3]:
                top_bids.append({"price": lvl.get("price"), "size": lvl.get("size")})
            for lvl in (book.get("asks") or [])[:3]:
                top_asks.append({"price": lvl.get("price"), "size": lvl.get("size")})

            joined.append({
                "outcome": mapping.get("outcome"),
                "token_id": token_id,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "midpoint": midpoint,
                "spread": spread,
                "display_price": display_price,
                "display_source": display_source,
                "last_trade_price": book.get("last_trade_price"),
                "executable_buy": executable.get("BUY"),
                "executable_sell": executable.get("SELL"),
                "tick_size": book.get("tick_size"),
                "min_order_size": book.get("min_order_size"),
                "top_bids": top_bids,
                "top_asks": top_asks,
                "raw_book_id": _raw_book_id(book) if book else None,
                "has_raw_book": bool(book),
            })

        result[slot_name] = {
            "item": slot["item"],
            "meta": meta,
            "books": joined,
        }
    return result


def _current_secs_to_end(start_secs: Optional[int], started_at: float) -> Optional[int]:
    if start_secs is None:
        return None
    elapsed = time.time() - started_at
    return max(0, int(round(start_secs - elapsed)))


def _slot_snapshot(slot_state: Dict[str, Any], slot_name: str) -> Dict[str, Any]:
    slot = slot_state.get(slot_name)
    if not slot:
        return {"up": None, "down": None}
    up = None
    down = None
    for item in slot.get("books") or []:
        outcome = str(item.get("outcome") or "").lower()
        if outcome == "up":
            up = item
        elif outcome == "down":
            down = item
    return {"up": up, "down": down}


def _compute_display_metrics(snap: Dict[str, Any]) -> Tuple[Optional[Dict[str, float]], str]:
    up = snap.get("up")
    down = snap.get("down")
    if not up or not down:
        return None, "missing_up_or_down_outcome"
    up_display = up.get("display_price")
    down_display = down.get("display_price")
    if up_display is None or down_display is None:
        return None, "missing_display_price"
    try:
        up_display = float(up_display)
        down_display = float(down_display)
    except Exception:
        return None, "invalid_display_price"
    return {
        "up_bid": round(up_display, 4),
        "up_ask": round(up_display, 4),
        "down_bid": round(down_display, 4),
        "down_ask": round(down_display, 4),
        "sum_asks": round(up_display + down_display, 4),
        "sum_bids": round(up_display + down_display, 4),
        "edge_asks": round(1 - (up_display + down_display), 4),
        "edge_bids": round((up_display + down_display) - 1, 4),
    }, "ok"


def _compute_executable_metrics(snap: Dict[str, Any]) -> Tuple[Optional[Dict[str, float]], str]:
    up = snap.get("up")
    down = snap.get("down")
    if not up or not down:
        return None, "missing_up_or_down_outcome"
    vals = [up.get("executable_buy"), up.get("executable_sell"), down.get("executable_buy"), down.get("executable_sell")]
    if any(v is None for v in vals):
        return None, "missing_executable_buy_or_sell"
    try:
        up_ask = float(up["executable_buy"])
        up_bid = float(up["executable_sell"])
        down_ask = float(down["executable_buy"])
        down_bid = float(down["executable_sell"])
    except Exception:
        return None, "invalid_executable_price"
    return {
        "up_bid": round(up_bid, 4),
        "up_ask": round(up_ask, 4),
        "down_bid": round(down_bid, 4),
        "down_ask": round(down_ask, 4),
        "sum_asks": round(up_ask + down_ask, 4),
        "sum_bids": round(up_bid + down_bid, 4),
        "edge_asks": round(1 - (up_ask + down_ask), 4),
        "edge_bids": round((up_bid + down_bid) - 1, 4),
    }, "ok"


def _allow_tradable_for_slot(slot_name: str, executor: Setup1BrokerExecutorV3, next_1_secs_to_end: Optional[int]) -> Tuple[bool, str]:
    if slot_name == "next_1":
        return True, "primary_focus"
    next_1_active = executor.slots["next_1"].active_plan_id is not None
    if next_1_active:
        return False, "next_1_plan_active"
    if next_1_secs_to_end is None or next_1_secs_to_end > UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1:
        return False, "next_2_waiting_for_roll_forward_window"
    return True, "roll_forward_window"


def _print_slot_debug(slot_name: str, snap: Dict[str, Any], display_reason: str, executable_reason: str, tradable_gate_reason: str) -> None:
    up = snap.get("up")
    down = snap.get("down")
    if up:
        print(f"[{slot_name.upper()} DEBUG] UP display={up.get('display_price')} source={up.get('display_source')} mid={up.get('midpoint')} spread={up.get('spread')} exec_buy={up.get('executable_buy')} exec_sell={up.get('executable_sell')} ltp={up.get('last_trade_price')}")
    if down:
        print(f"[{slot_name.upper()} DEBUG] DOWN display={down.get('display_price')} source={down.get('display_source')} mid={down.get('midpoint')} spread={down.get('spread')} exec_buy={down.get('executable_buy')} exec_sell={down.get('executable_sell')} ltp={down.get('last_trade_price')}")
    print(f"[{slot_name.upper()} DEBUG] display_reason={display_reason} | executable_reason={executable_reason} | tradable_gate={tradable_gate_reason}")


def monitor_setup1_shadow_public_rest_v4(duration_seconds: int = 60) -> None:
    broker = DryRunBroker()
    executor = Setup1BrokerExecutorV3(broker=broker, shadow_only=False)

    print("[BROKER_HEALTH]", broker.healthcheck().as_dict())
    slot_bundle = _build_slot_bundle()
    print("[QUEUE] 5m queue summary:")
    for slot_name in ("current", "next_1", "next_2"):
        item = slot_bundle["queue"].get(slot_name)
        if item:
            print(f"- {slot_name}: {item['seconds_to_end']}s | {item['title']} | slug={item['slug']}")
        else:
            print(f"- {slot_name}: none")

    stable_counts = {"current": 0, "next_1": 0, "next_2": 0}
    started_at = time.time()
    next_print = 0.0

    while time.time() - started_at < duration_seconds:
        slot_state = _fetch_slot_state(slot_bundle)

        if time.time() >= next_print:
            next_print = time.time() + POLL_INTERVAL_SECONDS
            print("\n===== SETUP1 SHADOW PUBLIC REST SNAPSHOT V4 =====")
            next_1_item = slot_bundle["queue"].get("next_1")
            next_1_secs = _current_secs_to_end(next_1_item.get("seconds_to_end") if next_1_item else None, started_at)

            for slot_name in ("current", "next_1", "next_2"):
                item = slot_bundle["queue"].get(slot_name)
                secs = _current_secs_to_end(item.get("seconds_to_end") if item else None, started_at)
                snap = _slot_snapshot(slot_state, slot_name)
                display_metrics, display_reason = _compute_display_metrics(snap)
                executable_metrics, executable_reason = _compute_executable_metrics(snap)

                if display_metrics:
                    stable_counts[slot_name] += 1
                else:
                    stable_counts[slot_name] = 0
                signal = classify_signal(display_metrics, stable_counts[slot_name], MIN_STABLE_SNAPSHOTS)

                tradable_allowed, tradable_gate_reason = _allow_tradable_for_slot(slot_name, executor, next_1_secs)
                tradable_metrics = None
                if executable_metrics and executable_metrics["sum_asks"] <= ARBITRAGE_SUM_ASKS_MAX and tradable_allowed:
                    tradable_metrics = executable_metrics

                print(f"\n[{slot_name.upper()}] secs_to_end={secs} | stable_count={stable_counts[slot_name]} | signal={signal}")
                if display_metrics:
                    print(f"DISPLAY asks={display_metrics['up_ask']}/{display_metrics['down_ask']} | bids={display_metrics['up_bid']}/{display_metrics['down_bid']} | sum_asks={display_metrics['sum_asks']} | sum_bids={display_metrics['sum_bids']}")
                else:
                    print(f"display_metrics=None | reason={display_reason}")
                if executable_metrics:
                    print(f"EXECUTABLE asks={executable_metrics['up_ask']}/{executable_metrics['down_ask']} | bids={executable_metrics['up_bid']}/{executable_metrics['down_bid']} | sum_asks={executable_metrics['sum_asks']} | sum_bids={executable_metrics['sum_bids']}")
                else:
                    print(f"executable_metrics=None | reason={executable_reason}")
                _print_slot_debug(slot_name, snap, display_reason, executable_reason, tradable_gate_reason)

                if slot_name in ("next_1", "next_2") and item:
                    if tradable_metrics is not None:
                        logs = executor.process_market_tick(
                            slot_name=slot_name,
                            event_slug=item["slug"],
                            signal=signal,
                            metrics=tradable_metrics,
                            secs_to_end=secs,
                            deadline_trigger=UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1 if slot_name == "next_1" else None,
                        )
                        for line in logs:
                            print(line)
                    else:
                        if signal in ("watching", "armed"):
                            print(f"[DISPLAY_ONLY] {slot_name}: tradable confirmation not satisfied")

            print("\n[EXECUTOR_SNAPSHOT]")
            print(executor.snapshot())
            print("\n[OPEN_ORDERS]")
            print([o.as_dict() for o in broker.get_open_orders()])

        time.sleep(POLL_INTERVAL_SECONDS)
