import requests
from datetime import datetime, timezone
from config.settings import GAMMA_API

BTC_HINTS = [
    "btc",
    "bitcoin",
    "btc-updown",
    "updown-5m",
    "updown-15m",
    "updown-1h",
    "up or down",
]


def fetch_active_events(limit=200, offset=0):
    url = f"{GAMMA_API}/events"
    params = {
        "active": "true",
        "closed": "false",
        "order": "end_date",
        "ascending": "true",
        "limit": limit,
        "offset": offset,
    }
    print(f"[API] Fetching active events from {url} | params={params}")

    try:
        res = requests.get(url, params=params, timeout=20)
        res.raise_for_status()
        data = res.json()
        print(f"[API] Active events received: {len(data)}")
        return data
    except Exception as e:
        print(f"[ERROR] Failed to fetch active events: {e}")
        return []


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _matches_blob(parts):
    blob = " ".join(str(p or "") for p in parts).lower()
    return any(k in blob for k in BTC_HINTS)


def _event_matches_btc(event):
    return _matches_blob([
        event.get("title"),
        event.get("ticker"),
        event.get("slug"),
        event.get("seriesSlug"),
    ])


def _market_matches_btc(market):
    return _matches_blob([
        market.get("question"),
        market.get("slug"),
        market.get("ticker"),
        market.get("groupItemTitle"),
    ])


def extract_relevant_btc_markets_from_events(events, max_seconds_ahead=14400):
    now = datetime.now(timezone.utc)
    results = []

    for event in events:
        event_markets = event.get("markets") or []

        if not _event_matches_btc(event) and not any(_market_matches_btc(m) for m in event_markets):
            continue

        for market in event_markets:
            if not _market_matches_btc(market):
                continue

            if market.get("active") is False or market.get("closed") is True:
                continue

            end_dt = _parse_dt(market.get("endDate")) or _parse_dt(event.get("endDate"))
            if not end_dt:
                continue

            secs = (end_dt - now).total_seconds()
            if 0 < secs <= max_seconds_ahead:
                results.append({
                    "event_title": event.get("title"),
                    "event_slug": event.get("slug"),
                    "market_question": market.get("question"),
                    "market_slug": market.get("slug"),
                    "endDate": market.get("endDate") or event.get("endDate"),
                    "active": market.get("active"),
                    "closed": market.get("closed"),
                    "seconds_to_end": round(secs),
                })

    results.sort(key=lambda x: x.get("seconds_to_end", 999999))
    print(f"[FILTER] Relevant BTC markets from active events: {len(results)}")
    return results


def print_debug_event_samples(events, limit=15):
    print("\n[DEBUG] Sample active event titles/slugs:")
    for event in events[:limit]:
        print(
            "-",
            event.get("title"),
            "| slug=",
            event.get("slug"),
            "| endDate=",
            event.get("endDate"),
        )
