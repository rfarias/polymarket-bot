import re
import time
from typing import List, Dict, Any

import requests

from config.settings import GAMMA_API
from market.page_discovery import discover_btc_fast_market_links

_EVENT_TTL_SECONDS = 2.0
_EVENT_CACHE: Dict[str, tuple[float, Dict[str, Any] | None]] = {}


def normalize_event_slug(link: str) -> str | None:
    if not link:
        return None

    # remove querystring
    link = link.split("?", 1)[0]
    # remove trailing /live
    link = re.sub(r"/live/?$", "", link)

    m = re.search(r"/event/([^/]+)$", link)
    if not m:
        return None
    return m.group(1)


def discover_unique_event_slugs() -> List[str]:
    links = discover_btc_fast_market_links()
    seen = set()
    slugs = []

    for link in links:
        slug = normalize_event_slug(link)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)

    print(f"[SLUG] Unique event slugs found: {len(slugs)}")
    return slugs


def fetch_event_by_slug(slug: str) -> Dict[str, Any] | None:
    cached = _EVENT_CACHE.get(slug)
    now = time.monotonic()
    if cached and now - cached[0] <= _EVENT_TTL_SECONDS:
        return cached[1]

    url = f"{GAMMA_API}/events/slug/{slug}"
    print(f"[API] Fetching event by slug: {slug}")

    try:
        res = requests.get(url, timeout=20)
        res.raise_for_status()
        data = res.json()
        _EVENT_CACHE[slug] = (now, data)
        return data
    except Exception as e:
        print(f"[ERROR] Failed to fetch event slug={slug}: {e}")
        _EVENT_CACHE[slug] = (now, None)
        return None


def classify_timeframe_from_slug(slug: str) -> str:
    s = slug.lower()
    if "5m" in s:
        return "5m"
    if "15m" in s:
        return "15m"
    if "1h" in s or "11am" in s or "10am" in s or "12pm" in s or "hour" in s:
        return "1h"
    if "on-" in s:
        return "daily"
    return "unknown"


def fetch_btc_fast_events() -> List[Dict[str, Any]]:
    slugs = discover_unique_event_slugs()
    results = []

    for slug in slugs:
        event = fetch_event_by_slug(slug)
        if not event:
            continue
        event["_derived_timeframe"] = classify_timeframe_from_slug(slug)
        results.append(event)

    print(f"[EVENTS] BTC fast events fetched by slug: {len(results)}")
    return results
