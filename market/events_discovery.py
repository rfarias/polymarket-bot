import requests
from datetime import datetime, timezone
from config.settings import GAMMA_API


def fetch_active_events(limit=100, offset=0):
    url = f"{GAMMA_API}/events"
    params = {
        "active": "true",
        "closed": "false",
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


def _event_matches_btc(event):
    text_parts = [
        str(event.get("title", "")),
        str(event.get("ticker", "")),
        str(event.get("slug", "")),
    ]
    blob = " ".join(text_parts).lower()
    return any(k in blob for k in ["btc", "bitcoin", "up or down"])


def _market_matches_btc(market):
    text_parts = [
        str(market.get("question", "")),
        str(market.get("slug", "")),
        str(market.get("ticker", "")),
    ]
    blob = " ".join(text_parts).lower()
    return any(k in blob for k in ["btc", "bitcoin"])


def extract_relevant_btc_markets_from_events(events, max_seconds_ahead=7200):
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
