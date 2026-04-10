import time
from typing import Any, Dict, Optional, Tuple

from market.book_5m import fetch_books_for_tokens, fetch_market_metadata_from_slug
from market.queue_5m_v5 import build_5m_queue_v5, UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1
from market.setup1_policy import classify_signal
from market.setup1_broker_executor_v3 import Setup1BrokerExecutorV3
from market.dryrun_broker import DryRunBroker
from market.public_market_data_v1 import fetch_midpoints, fetch_prices, fetch_spread

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


def _fetch_slot_books(slot_bundle: Dict[str, Any]) -> Dict[str, Any]:
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
        prices = fetch_prices(token_ids + token_ids, ["BUY"] * len(token_ids) + ["SELL"] * len(token_ids))
        joined = []
        for mapping in token_mapping:
            token_id = str(mapping["token_id"])
            book = by_id.get(token_id) or {}
            best_bid = _best_bid(book)
            best_ask = _best_ask(book)
            price_map = prices.get(token_id) or {}
            spread = fetch_spread(token_id)
            midpoint = midpoints.get(token_id)
            display_price, display_source = _display_price(midpoint, spread, book.get("last_trade_price"))
            joined.append({
                "outcome": mapping.get("outcome"),
                "token_id": token_id,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "public_buy_price": price_map.get("BUY"),
                "public_sell_price": price_map.get("SELL"),
                "midpoint": midpoint,
                "spread": spread,
                "display_price": display_price,
                "display_source": display_source,
                "last_trade_price": book.get("last_trade_price"),
                "tick_size": book.get("tick_size"),
                "min_order_size": book.get("min_order_size"),
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


def _slot_snapshot(books_state: Dict[str, Any], slot_name: str) -> Dict[str, Any]:
    slot = books_state.get(slot_name)
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


def _compute_metrics_from_display_prices(snap: Dict[str, Any]) -> Tuple[Optional[Dict[str, float]], str]:
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

    synthetic_up_bid = up.get("public_sell_price") if up.get("public_sell_price") is not None else up_display
    synthetic_up_ask = up.get("public_buy_price") if up.get("public_buy_price") is not None else up_display
    synthetic_down_bid = down.get("public_sell_price") if down.get("public_sell_price") is not None else down_display
    synthetic_down_ask = down.get("public_buy_price") if down.get("public_buy_price") is not None else down_display

    try:
        up_bid = float(synthetic_up_bid)
        up_ask = float(synthetic_up_ask)
        down_bid = float(synthetic_down_bid)
        down_ask = float(synthetic_down_ask)
    except Exception:
        return None, "invalid_synthetic_price"

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


def _print_slot_debug(slot_name: str, snap: Dict[str, Any], metrics_reason: str) -> None:
    up = snap.get("up")
    down = snap.get("down")
    if up:
        print(f"[{slot_name.upper()} DEBUG] UP display={up.get('display_price')} source={up.get('display_source')} mid={up.get('midpoint')} spread={up.get('spread')} buy={up.get('public_buy_price')} sell={up.get('public_sell_price')} ltp={up.get('last_trade_price')}")
    if down:
        print(f"[{slot_name.upper()} DEBUG] DOWN display={down.get('display_price')} source={down.get('display_source')} mid={down.get('midpoint')} spread={down.get('spread')} buy={down.get('public_buy_price')} sell={down.get('public_sell_price')} ltp={down.get('last_trade_price')}")
    print(f"[{slot_name.upper()} DEBUG] metrics_reason={metrics_reason}")


def monitor_setup1_shadow_public_rest_v3(duration_seconds: int = 60) -> None:
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
        books_state = _fetch_slot_books(slot_bundle)

        if time.time() >= next_print:
            next_print = time.time() + POLL_INTERVAL_SECONDS
            print("\n===== SETUP1 SHADOW PUBLIC REST SNAPSHOT V3 =====")
            for slot_name in ("current", "next_1", "next_2"):
                item = slot_bundle["queue"].get(slot_name)
                secs = _current_secs_to_end(item.get("seconds_to_end") if item else None, started_at)
                snap = _slot_snapshot(books_state, slot_name)
                metrics, metrics_reason = _compute_metrics_from_display_prices(snap)
                if metrics:
                    stable_counts[slot_name] += 1
                else:
                    stable_counts[slot_name] = 0
                signal = classify_signal(metrics, stable_counts[slot_name], MIN_STABLE_SNAPSHOTS)

                print(f"\n[{slot_name.upper()}] secs_to_end={secs} | stable_count={stable_counts[slot_name]} | signal={signal}")
                if metrics:
                    print(f"UP/DOWN asks={metrics['up_ask']}/{metrics['down_ask']} | bids={metrics['up_bid']}/{metrics['down_bid']}")
                    print(f"SUM_ASKS={metrics['sum_asks']} | SUM_BIDS={metrics['sum_bids']}")
                else:
                    print(f"metrics=None | reason={metrics_reason}")
                _print_slot_debug(slot_name, snap, metrics_reason)

                if slot_name in ("next_1", "next_2") and item:
                    logs = executor.process_market_tick(
                        slot_name=slot_name,
                        event_slug=item["slug"],
                        signal=signal,
                        metrics=metrics,
                        secs_to_end=secs,
                        deadline_trigger=UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1 if slot_name == "next_1" else None,
                    )
                    for line in logs:
                        print(line)

            print("\n[EXECUTOR_SNAPSHOT]")
            print(executor.snapshot())
            print("\n[OPEN_ORDERS]")
            print([o.as_dict() for o in broker.get_open_orders()])

        time.sleep(POLL_INTERVAL_SECONDS)
