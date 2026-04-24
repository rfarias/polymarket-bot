import json
import time
from typing import Any, Dict, List, Optional

import requests

from config.settings import GAMMA_API
from market.queue_5m_v2 import build_5m_queue_v2
from market.slug_discovery import fetch_event_by_slug

CLOB_API = "https://clob.polymarket.com"
BOOK_TIMEOUT = 3.0
_META_TTL_SECONDS = 5.0
_META_CACHE: Dict[str, tuple[float, Dict[str, Any] | None]] = {}


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def fetch_market_metadata_from_slug(slug: str) -> Optional[Dict[str, Any]]:
    cached = _META_CACHE.get(slug)
    now = time.monotonic()
    if cached and now - cached[0] <= _META_TTL_SECONDS:
        return cached[1]

    event = fetch_event_by_slug(slug)
    if not event:
        _META_CACHE[slug] = (now, None)
        return None

    markets = event.get("markets") or []
    if not markets:
        _META_CACHE[slug] = (now, None)
        return None

    market = markets[0]
    token_ids = _as_list(market.get("clobTokenIds"))
    outcomes = _as_list(market.get("outcomes"))

    mapping = []
    for i, token_id in enumerate(token_ids):
        outcome = outcomes[i] if i < len(outcomes) else f"OUTCOME_{i}"
        mapping.append({
            "outcome": outcome,
            "token_id": str(token_id),
        })

    meta = {
        "event_title": event.get("title"),
        "event_slug": event.get("slug"),
        "market_question": market.get("question"),
        "market_slug": market.get("slug"),
        "token_mapping": mapping,
        "enableOrderBook": market.get("enableOrderBook"),
        "acceptingOrders": market.get("acceptingOrders"),
        "liquidityClob": market.get("liquidityClob"),
        "volumeClob": market.get("volumeClob"),
        "endDate": market.get("endDate") or event.get("endDate"),
    }
    _META_CACHE[slug] = (now, meta)
    return meta


def fetch_books_for_tokens(token_ids: List[str]) -> List[Dict[str, Any]]:
    if not token_ids:
        return []

    url = f"{CLOB_API}/books"
    payload = [{"token_id": t} for t in token_ids]
    print(f"[BOOK] Fetching {len(token_ids)} token books from {url}")

    try:
        res = requests.post(url, json=payload, timeout=BOOK_TIMEOUT)
        res.raise_for_status()
        data = res.json()
        print(f"[BOOK] Books received: {len(data)}")
        return data
    except Exception as e:
        print(f"[ERROR] Failed to fetch books: {e}")
        return []


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


def _summarize_book(book: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "asset_id": book.get("asset_id"),
        "best_bid": _best_bid(book),
        "best_ask": _best_ask(book),
        "tick_size": book.get("tick_size"),
        "min_order_size": book.get("min_order_size"),
        "last_trade_price": book.get("last_trade_price"),
    }


def fetch_5m_queue_with_books() -> Dict[str, Any]:
    queue = build_5m_queue_v2()
    result: Dict[str, Any] = {
        "queue": queue,
        "slots": {},
    }

    for slot_name in ("current", "next_1", "next_2"):
        item = queue.get(slot_name)
        if not item:
            result["slots"][slot_name] = None
            continue

        meta = fetch_market_metadata_from_slug(item["slug"])
        if not meta:
            result["slots"][slot_name] = {
                "item": item,
                "meta": None,
                "books": [],
            }
            continue

        token_ids = [x["token_id"] for x in meta["token_mapping"] if x.get("token_id")]
        raw_books = fetch_books_for_tokens(token_ids)
        summarized = [_summarize_book(b) for b in raw_books]

        # casar summaries com outcomes
        joined = []
        for mapping in meta["token_mapping"]:
            token_id = mapping["token_id"]
            summary = next((b for b in summarized if str(b.get("asset_id")) == str(token_id)), None)
            joined.append({
                "outcome": mapping["outcome"],
                "token_id": token_id,
                "book": summary,
            })

        result["slots"][slot_name] = {
            "item": item,
            "meta": meta,
            "books": joined,
        }

    return result
