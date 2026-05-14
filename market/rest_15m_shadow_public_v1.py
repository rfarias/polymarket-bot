from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict

from market.book_5m import fetch_books_for_tokens, fetch_market_metadata_from_slug
from market.public_market_data_v1 import fetch_midpoints
from market.public_market_data_v2 import fetch_token_executable_prices
from market.queue_15m_v1 import build_15m_queue_v1
from market.rest_5m_shadow_public_v4 import (
    _best_ask,
    _best_bid,
    _computed_spread,
    _display_price,
    _raw_book_id,
    _slot_snapshot,
)


def build_slot_bundle_15m_v1() -> Dict[str, Any]:
    queue = build_15m_queue_v1()
    slots: Dict[str, Any] = {}
    for slot_name in ("current", "next_1"):
        item = queue.get(slot_name)
        if not item:
            slots[slot_name] = None
            continue
        meta = fetch_market_metadata_from_slug(item["slug"])
        if not meta:
            slots[slot_name] = None
            continue
        slots[slot_name] = {"item": item, "meta": meta}
    return {"queue": queue, "slots": slots}


def fetch_slot_state_15m_v1(slot_bundle: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for slot_name, slot in slot_bundle["slots"].items():
        if not slot:
            result[slot_name] = None
            continue

        meta = slot["meta"]
        token_mapping = meta.get("token_mapping") or []
        token_ids = [str(x["token_id"]) for x in token_mapping if x.get("token_id")]
        with ThreadPoolExecutor(max_workers=max(2, len(token_ids) + 2)) as pool:
            books_future = pool.submit(fetch_books_for_tokens, token_ids)
            midpoints_future = pool.submit(fetch_midpoints, token_ids)
            executable_futures = {
                token_id: pool.submit(fetch_token_executable_prices, token_id)
                for token_id in token_ids
            }
            raw_books = books_future.result()
            midpoints = midpoints_future.result()
            executable_prices = {
                token_id: future.result()
                for token_id, future in executable_futures.items()
            }

        by_id = {_raw_book_id(book): book for book in raw_books}
        joined = []
        for mapping in token_mapping:
            token_id = str(mapping["token_id"])
            book = by_id.get(token_id) or {}
            best_bid = _best_bid(book)
            best_ask = _best_ask(book)
            midpoint = midpoints.get(token_id)
            spread = _computed_spread(best_bid, best_ask)
            executable = executable_prices.get(token_id) or {}
            display_price, display_source = _display_price(midpoint, spread, book.get("last_trade_price"))
            top_bids = [{"price": lvl.get("price"), "size": lvl.get("size")} for lvl in (book.get("bids") or [])[:3]]
            top_asks = [{"price": lvl.get("price"), "size": lvl.get("size")} for lvl in (book.get("asks") or [])[:3]]
            joined.append(
                {
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
                }
            )

        result[slot_name] = {"item": slot["item"], "meta": meta, "books": joined}
    return result


def slot_snapshot_15m_v1(slot_state: Dict[str, Any], slot_name: str = "current") -> Dict[str, Any]:
    return _slot_snapshot(slot_state, slot_name)
