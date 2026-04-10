from datetime import datetime, timezone
from typing import Any, Dict, Optional

from market.slug_discovery import fetch_event_by_slug

FIVE_MINUTE_STEP = 300
UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1 = 330


def _round_up_to_next_5m_epoch(now_ts: int) -> int:
    return ((now_ts // FIVE_MINUTE_STEP) + 1) * FIVE_MINUTE_STEP


def _parse_dt(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _normalize_5m_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not event:
        return None
    slug = str(event.get("slug") or "")
    if not slug.startswith("btc-updown-5m-"):
        return None

    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]

    if market.get("active") is not True:
        return None
    if market.get("closed") is True:
        return None
    if market.get("acceptingOrders") is not True:
        return None
    if market.get("enableOrderBook") is not True:
        return None

    end_dt = _parse_dt(event.get("endDate") or market.get("endDate"))
    if not end_dt:
        return None

    now = datetime.now(timezone.utc)
    secs_to_end = (end_dt - now).total_seconds()
    if secs_to_end <= 0:
        return None

    return {
        "title": event.get("title"),
        "slug": slug,
        "market_slug": market.get("slug"),
        "seconds_to_end": round(secs_to_end),
        "endDate": event.get("endDate") or market.get("endDate"),
        "acceptingOrders": market.get("acceptingOrders"),
        "enableOrderBook": market.get("enableOrderBook"),
    }


def _fetch_target(ts: int) -> Optional[Dict[str, Any]]:
    slug = f"btc-updown-5m-{ts}"
    print(f"[5M] Fetching direct target slug: {slug}")
    event = fetch_event_by_slug(slug)
    normalized = _normalize_5m_event(event) if event else None
    if normalized:
        print(f"[5M] Direct target ready: {slug} | secs_to_end={normalized['seconds_to_end']}")
    else:
        print(f"[5M] Direct target unavailable: {slug}")
    return normalized


def build_5m_queue_v5() -> Dict[str, Optional[Dict[str, Any]]]:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    current_end_ts = _round_up_to_next_5m_epoch(now_ts)

    current = _fetch_target(current_end_ts)
    next_1 = _fetch_target(current_end_ts + FIVE_MINUTE_STEP)
    next_2 = _fetch_target(current_end_ts + 2 * FIVE_MINUTE_STEP)

    return {
        "current": current,
        "next_1": next_1,
        "next_2": next_2,
        "unfilled_exit_trigger_secs_to_end_on_next_1": UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1,
    }
