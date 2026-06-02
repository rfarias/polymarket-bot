from __future__ import annotations

import json
import time
from typing import Any, Optional

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "ev-scanner/1.0"})

_PAGE_SIZE = 100   # max per request
_MAX_PAGES  = 10   # cap at 1000 events total per scan


def _get(url: str, params: dict | None = None, timeout: int = 15) -> Any:
    resp = _SESSION.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _event_tags(event: dict) -> set[str]:
    """Return set of lowercased tag slugs and labels for an event."""
    tags = event.get("tags") or []
    result = set()
    for t in tags:
        if isinstance(t, dict):
            result.add(str(t.get("slug", "")).lower())
            result.add(str(t.get("label", "")).lower())
        elif isinstance(t, str):
            result.add(t.lower())
    return result


def get_active_markets(tag: Optional[str] = None, tag_slug: Optional[str] = None, limit: int = 500) -> list[dict]:
    """Fetch active markets with pagination.
    tag_slug: passed as query param (e.g. 'daily-temperature', 'fed-rates').
    tag: filtered client-side from nested tags objects."""
    all_events: list[dict] = []
    offset = 0
    pages = max(1, min(_MAX_PAGES, (limit + _PAGE_SIZE - 1) // _PAGE_SIZE))

    for _ in range(pages):
        params: dict = {
            "active": "true",
            "closed": "false",
            "limit": _PAGE_SIZE,
            "offset": offset,
            "order": "volume",
            "ascending": "false",
        }
        if tag_slug:
            params["tag_slug"] = tag_slug
        try:
            data = _get(f"{GAMMA_BASE}/events", params=params)
        except Exception:
            break

        page = data if isinstance(data, list) else data.get("events", data.get("data", []))
        if not page:
            break
        all_events.extend(page)
        offset += len(page)
        if len(page) < _PAGE_SIZE:
            break
        time.sleep(0.1)

    if tag:
        tag_lower = tag.lower()
        all_events = [e for e in all_events if tag_lower in _event_tags(e)]

    return all_events[:limit]


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
    prices = m.get("outcomePrices") or []
    yes_price = float(prices[0]) if prices else 0.5
    return {"yes": yes_price, "no": round(1.0 - yes_price, 6), "slug": market_slug}


def get_market_volume(market_slug: str) -> float:
    data = _get(f"{GAMMA_BASE}/events", params={"slug": market_slug, "limit": 1})
    events = data if isinstance(data, list) else data.get("events", [])
    if not events:
        return 0.0
    return float(events[0].get("volume", 0) or 0)


def search_markets(keywords: list[str], tag: Optional[str] = None, tag_slug: Optional[str] = None, limit: int = 500) -> list[dict]:
    """Fetch active markets and filter by keywords in slug, title, or tags."""
    markets = get_active_markets(tag=tag, tag_slug=tag_slug, limit=limit)
    kws = [kw.lower() for kw in keywords]
    results = []
    for event in markets:
        slug   = str(event.get("slug")  or "").lower()
        title  = str(event.get("title") or "").lower()
        tags   = _event_tags(event)
        text   = slug + " " + title + " " + " ".join(tags)
        if any(kw in text for kw in kws):
            results.append(event)
    return results


def parse_list_field(value: Any) -> list:
    """Gamma API returns some list fields as JSON strings. Normalize to list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def browse_tags(sample: int = 300) -> dict[str, int]:
    """Helper: count tag frequencies in a sample of active markets."""
    markets = get_active_markets(limit=sample)
    counts: dict[str, int] = {}
    for e in markets:
        for t in _event_tags(e):
            counts[t] = counts.get(t, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))
