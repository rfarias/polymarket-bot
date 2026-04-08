import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from market.slug_discovery import discover_unique_event_slugs, fetch_event_by_slug

FIVE_MINUTE_STEP = 300
UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1 = 330
MAX_ANCHOR_HORIZON_SECS = 20 * 60
MAX_NEXT_HORIZON_SECS = 30 * 60


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


def discover_5m_candidate_slugs() -> List[str]:
    slugs = discover_unique_event_slugs()
    only_5m = [s for s in slugs if s.startswith("btc-updown-5m-")]
    only_5m.sort(key=lambda s: _extract_5m_timestamp_from_slug(s) or 0)
    print(f"[5M] Candidate 5m slugs discovered: {len(only_5m)}")
    return only_5m


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

    base_ts = _extract_5m_timestamp_from_slug(slug)
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


def fetch_5m_live_candidates() -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for slug in discover_5m_candidate_slugs():
        event = fetch_event_by_slug(slug)
        normalized = _normalize_5m_event(event) if event else None
        if normalized:
            candidates.append(normalized)

    candidates.sort(key=lambda x: x["seconds_to_end"])
    print(f"[5M] Live candidate events ready: {len(candidates)}")
    return candidates


def choose_anchor_5m_event(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not candidates:
        print("[5M] No live 5m candidates available")
        return None

    near = [c for c in candidates if c["seconds_to_end"] <= MAX_ANCHOR_HORIZON_SECS]
    if not near:
        print(f"[5M] No 5m candidate inside anchor horizon ({MAX_ANCHOR_HORIZON_SECS}s)")
        return None

    anchor = near[0]
    print(f"[5M] Anchor 5m event selected: {anchor['slug']} | secs_to_end={anchor['seconds_to_end']}")
    return anchor


def _fetch_or_fallback_derived(base_ts: int, step_index: int, known_candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    target_ts = base_ts + (FIVE_MINUTE_STEP * step_index)
    target_slug = f"btc-updown-5m-{target_ts}"

    for item in known_candidates:
        if item["slug"] == target_slug:
            if item["seconds_to_end"] <= MAX_NEXT_HORIZON_SECS:
                print(f"[5M] Derived event found in discovered candidates: {target_slug} | secs_to_end={item['seconds_to_end']}")
                return item
            print(f"[5M] Derived discovered candidate too far: {target_slug} | secs_to_end={item['seconds_to_end']}")
            return None

    event = fetch_event_by_slug(target_slug)
    normalized = _normalize_5m_event(event) if event else None
    if normalized and normalized["seconds_to_end"] <= MAX_NEXT_HORIZON_SECS:
        print(f"[5M] Derived event fetched directly: {target_slug} | secs_to_end={normalized['seconds_to_end']}")
        return normalized

    print(f"[5M] Derived event unavailable or too far: {target_slug}")
    return None


def build_5m_queue_v3() -> Dict[str, Optional[Dict[str, Any]]]:
    candidates = fetch_5m_live_candidates()
    current = choose_anchor_5m_event(candidates)
    if not current:
        return {
            "current": None,
            "next_1": None,
            "next_2": None,
            "unfilled_exit_trigger_secs_to_end_on_next_1": UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1,
        }

    base_ts = current["base_ts"]
    next_1 = _fetch_or_fallback_derived(base_ts, 1, candidates)
    next_2 = _fetch_or_fallback_derived(base_ts, 2, candidates)

    return {
        "current": current,
        "next_1": next_1,
        "next_2": next_2,
        "unfilled_exit_trigger_secs_to_end_on_next_1": UNFILLED_EXIT_TRIGGER_SECS_TO_END_ON_NEXT_1,
    }
