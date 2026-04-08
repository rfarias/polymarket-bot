import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from market.slug_discovery import fetch_event_by_slug
from market.slug_discovery_v2 import fetch_operational_fast_events

FIVE_MINUTE_STEP = 300
CURRENT_5M_HORIZON = 20 * 60
UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1 = 330


def _parse_dt(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_5m_timestamp_from_slug(slug: str) -> Optional[int]:
    m = re.search(r"btc-updown-5m-(\d+)$", slug or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _normalize_5m_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not event:
        return None

    slug = event.get("slug")
    if not slug or "btc-updown-5m-" not in str(slug):
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

    base_ts = _extract_5m_timestamp_from_slug(str(slug))
    if not base_ts:
        return None

    return {
        "title": event.get("title"),
        "slug": slug,
        "market_slug": market.get("slug"),
        "seconds_to_end": round(secs_to_end),
        "endDate": event.get("endDate") or market.get("endDate"),
        "base_ts": base_ts,
        "acceptingOrders": market.get("acceptingOrders"),
        "enableOrderBook": market.get("enableOrderBook"),
    }


def find_current_5m_event() -> Optional[Dict[str, Any]]:
    events = fetch_operational_fast_events()
    five_min = [e for e in events if e.get("timeframe") == "5m" and e.get("seconds_to_end", 999999) <= CURRENT_5M_HORIZON]
    five_min.sort(key=lambda x: x["seconds_to_end"])

    if not five_min:
        print("[5M] No current 5m event found within horizon")
        return None

    current = five_min[0]
    base_ts = _extract_5m_timestamp_from_slug(str(current.get("slug")))
    if not base_ts:
        print(f"[5M] Could not parse base timestamp from current slug: {current.get('slug')}")
        return None

    current["base_ts"] = base_ts
    print(f"[5M] Current 5m event selected: {current['slug']} | secs_to_end={current['seconds_to_end']}")
    return current


def _fetch_derived_5m_event(base_ts: int, step_index: int) -> Optional[Dict[str, Any]]:
    target_ts = base_ts + (FIVE_MINUTE_STEP * step_index)
    slug = f"btc-updown-5m-{target_ts}"
    event = fetch_event_by_slug(slug)
    normalized = _normalize_5m_event(event) if event else None

    if normalized:
        print(f"[5M] Derived event ready: {slug} | secs_to_end={normalized['seconds_to_end']}")
    else:
        print(f"[5M] Derived event not ready or unavailable: {slug}")

    return normalized


def build_5m_queue() -> Dict[str, Optional[Dict[str, Any]]]:
    current = find_current_5m_event()
    if not current:
        return {
            "current": None,
            "next_1": None,
            "next_2": None,
            "unfilled_exit_trigger_secs_to_end_on_next_1": UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1,
        }

    base_ts = current["base_ts"]
    next_1 = _fetch_derived_5m_event(base_ts, 1)
    next_2 = _fetch_derived_5m_event(base_ts, 2)

    return {
        "current": current,
        "next_1": next_1,
        "next_2": next_2,
        "unfilled_exit_trigger_secs_to_end_on_next_1": UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1,
    }
