"""Generic queue builder for any coin/timeframe on Polymarket.

Supports any combination of coin (btc, eth, sol, xrp) and timeframe (5m, 15m).
Slug pattern: {coin}-updown-{timeframe}-{epoch_timestamp}
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from market.slug_discovery import fetch_event_by_slug

STEP_FOR_TF: Dict[str, int] = {"5m": 300, "15m": 900, "1h": 3600}


def _epoch_for_tf(timeframe: str, now_ts: int) -> int:
    step = STEP_FOR_TF.get(timeframe, 300)
    return (now_ts // step) * step


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _normalize_event(event: Dict[str, Any], coin: str, timeframe: str) -> Optional[Dict[str, Any]]:
    if not event:
        return None
    slug = str(event.get("slug") or "")
    expected_prefix = f"{coin}-updown-{timeframe}-"
    if not slug.startswith(expected_prefix):
        return None

    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]

    if market.get("active") is False or market.get("closed") is True:
        return None
    if not market.get("acceptingOrders"):
        return None
    if not market.get("enableOrderBook"):
        return None

    end_dt = _parse_dt(event.get("endDate") or market.get("endDate"))
    if not end_dt:
        return None
    now = datetime.now(timezone.utc)
    secs = (end_dt - now).total_seconds()
    if secs <= 0:
        return None

    return {
        "slug": slug,
        "market_slug": market.get("slug"),
        "seconds_to_end": round(secs),
        "coin": coin,
        "timeframe": timeframe,
        "endDate": event.get("endDate") or market.get("endDate"),
        "title": event.get("title", ""),
    }


def build_coin_queue(
    coin: str,
    timeframe: str,
    slots: int = 2,
    verbose: bool = False,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Returns {'current': {...}, 'next_1': {...}} for any coin/timeframe.

    Args:
        coin: e.g. 'btc', 'eth', 'sol', 'xrp'
        timeframe: e.g. '5m', '15m'
        slots: number of slots to fetch (current + N-1 next)
        verbose: print fetch info
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    step = STEP_FOR_TF.get(timeframe, 300)
    current_start = _epoch_for_tf(timeframe, now_ts)

    slot_names = ["current"] + [f"next_{i}" for i in range(1, slots)]
    result: Dict[str, Optional[Dict[str, Any]]] = {}

    for i, slot_name in enumerate(slot_names):
        ts = current_start + i * step
        slug = f"{coin}-updown-{timeframe}-{ts}"
        if verbose:
            print(f"[QUEUE] {coin.upper()}-{timeframe} {slot_name}: {slug}")
        event = fetch_event_by_slug(slug)
        result[slot_name] = _normalize_event(event, coin, timeframe) if event else None

    return result
