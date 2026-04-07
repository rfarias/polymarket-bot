import requests
from datetime import datetime, timezone
from config.settings import GAMMA_API

BTC_HINTS = [
    "btc",
    "bitcoin",
    "up or down",
    "updown",
    "5m",
    "15m",
    "1h",
]


def fetch_active_markets(limit=100, offset=0):
    url = f"{GAMMA_API}/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "offset": offset,
    }
    print(f"[API] Fetching active markets from {url} | params={params}")

    try:
        res = requests.get(url, params=params, timeout=20)
        res.raise_for_status()
        data = res.json()
        print(f"[API] Active markets received: {len(data)}")
        return data
    except Exception as e:
        print(f"[ERROR] Failed to fetch active markets: {e}")
        return []


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _matches_btc(market):
    parts = [
        market.get("question"),
        market.get("slug"),
        market.get("ticker"),
        market.get("groupItemTitle"),
        market.get("description"),
    ]
    blob = " ".join(str(p or "") for p in parts).lower()
    return any(k in blob for k in BTC_HINTS) and ("btc" in blob or "bitcoin" in blob)


def print_debug_market_samples(markets, limit=25):
    print("\n[DEBUG] Sample active market questions/slugs:")
    for market in markets[:limit]:
        print(
            "-",
            market.get("question"),
            "| slug=",
            market.get("slug"),
            "| endDate=",
            market.get("endDate"),
            "| enableOrderBook=",
            market.get("enableOrderBook"),
        )


def extract_relevant_btc_markets(markets, max_seconds_ahead=14400):
    now = datetime.now(timezone.utc)
    results = []

    for market in markets:
        if not _matches_btc(market):
            continue

        if market.get("active") is False or market.get("closed") is True:
            continue

        if market.get("enableOrderBook") is False:
            continue

        end_dt = _parse_dt(market.get("endDate"))
        if not end_dt:
            continue

        secs = (end_dt - now).total_seconds()
        if 0 < secs <= max_seconds_ahead:
            results.append({
                "question": market.get("question"),
                "slug": market.get("slug"),
                "endDate": market.get("endDate"),
                "seconds_to_end": round(secs),
                "active": market.get("active"),
                "closed": market.get("closed"),
                "enableOrderBook": market.get("enableOrderBook"),
            })

    results.sort(key=lambda x: x.get("seconds_to_end", 999999))
    print(f"[FILTER] Relevant BTC active markets: {len(results)}")
    return results
