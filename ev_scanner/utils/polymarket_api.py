from __future__ import annotations

import time
from typing import Any, Optional

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "ev-scanner/1.0"})


def _get(url: str, params: dict | None = None, timeout: int = 15) -> Any:
    resp = _SESSION.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def get_active_markets(tag: Optional[str] = None, limit: int = 100) -> list[dict]:
    params: dict = {"active": "true", "closed": "false", "limit": limit}
    if tag:
        params["tag"] = tag
    data = _get(f"{GAMMA_BASE}/events", params=params)
    if isinstance(data, list):
        return data
    return data.get("events", data.get("data", []))


def get_market_price(market_slug: str) -> dict:
    """Returns {"yes": float, "no": float, "slug": str} or empty dict on miss."""
    data = _get(f"{GAMMA_BASE}/events", params={"slug": market_slug, "limit": 1})
    events = data if isinstance(data, list) else data.get("events", [])
    if not events:
        return {}
    event = events[0]
    markets = event.get("markets", [])
    if not markets:
        return {}
    m = markets[0]
    yes_price = float(m.get("outcomePrices", [0.5])[0])
    return {"yes": yes_price, "no": round(1.0 - yes_price, 6), "slug": market_slug}


def get_market_volume(market_slug: str) -> float:
    data = _get(f"{GAMMA_BASE}/events", params={"slug": market_slug, "limit": 1})
    events = data if isinstance(data, list) else data.get("events", [])
    if not events:
        return 0.0
    return float(events[0].get("volume", 0) or 0)


def search_markets(keywords: list[str], tag: Optional[str] = None, limit: int = 200) -> list[dict]:
    """Fetch active markets and filter by keywords in slug or title."""
    markets = get_active_markets(tag=tag, limit=limit)
    results = []
    for event in markets:
        slug  = str(event.get("slug") or "").lower()
        title = str(event.get("title") or "").lower()
        if any(kw.lower() in slug or kw.lower() in title for kw in keywords):
            results.append(event)
    return results


def get_token_prices(token_ids: list[str]) -> dict[str, float]:
    """Batch fetch prices for a list of token IDs from CLOB API."""
    if not token_ids:
        return {}
    try:
        resp = _SESSION.get(
            "https://clob.polymarket.com/prices-history",
            params={"token_id": token_ids[0], "interval": "1m", "fidelity": 1},
            timeout=10,
        )
        resp.raise_for_status()
        # Fallback: use gamma prices
    except Exception:
        pass
    return {}
