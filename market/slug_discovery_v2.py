import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from market.slug_discovery import fetch_btc_fast_events


FAST_TIMEFRAMES = {"5m", "15m", "1h"}


def _parse_dt(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def classify_timeframe(event: Dict[str, Any]) -> str:
    slug = str(event.get("slug") or "").lower()
    title = str(event.get("title") or "").lower()

    # ordem importa: 15m antes de 5m
    if "15m" in slug or re.search(r"\b\d{1,2}:\d{2}[ap]m-\d{1,2}:\d{2}[ap]m\b", title) and ":15" in title:
        return "15m"

    if "5m" in slug:
        return "5m"

    # Títulos horários normalmente não têm faixa de minutos, ex.: 5PM ET
    if "1h" in slug:
        return "1h"

    if "up or down -" in title and re.search(r"\b\d{1,2}(?::\d{2})?[ap]m et\b", title):
        # se houver faixa com minutos, já teria caído nos casos acima; aqui tratamos como horário/1h
        if re.search(r"\b\d{1,2}[ap]m et\b", title) or re.search(r"\b\d{1,2}:\d{2}[ap]m et\b", title):
            if "-" not in title.split("up or down -", 1)[1]:
                return "1h"
            # faixa tipo 8:00pm-8:15pm et ou 8:20pm-8:25pm et
            m = re.search(r"(\d{1,2}:\d{2}[ap]m)-(\d{1,2}:\d{2}[ap]m) et", title)
            if m:
                start, end = m.groups()
                try:
                    def to_minutes(t: str) -> int:
                        hhmm = datetime.strptime(t, "%I:%M%p")
                        return hhmm.hour * 60 + hhmm.minute
                    diff = to_minutes(end) - to_minutes(start)
                    if diff == 5:
                        return "5m"
                    if diff == 15:
                        return "15m"
                except Exception:
                    pass

    return "unknown"


def normalize_fast_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    markets = event.get("markets") or []
    if not markets:
        return None

    market = markets[0]
    timeframe = classify_timeframe(event)
    if timeframe not in FAST_TIMEFRAMES:
        return None

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
        "slug": event.get("slug"),
        "timeframe": timeframe,
        "endDate": event.get("endDate") or market.get("endDate"),
        "seconds_to_end": round(secs_to_end),
        "market_question": market.get("question"),
        "market_slug": market.get("slug"),
        "acceptingOrders": market.get("acceptingOrders"),
        "enableOrderBook": market.get("enableOrderBook"),
    }


def fetch_operational_fast_events() -> List[Dict[str, Any]]:
    raw_events = fetch_btc_fast_events()
    normalized = []

    for event in raw_events:
        item = normalize_fast_event(event)
        if item:
            normalized.append(item)

    normalized.sort(key=lambda x: (x["timeframe"], x["seconds_to_end"]))
    print(f"[OPERATIONAL] Fast events ready: {len(normalized)}")
    return normalized


def split_current_and_next(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
    by_tf: Dict[str, List[Dict[str, Any]]] = {"5m": [], "15m": [], "1h": []}

    for ev in events:
        tf = ev["timeframe"]
        if tf in by_tf:
            by_tf[tf].append(ev)

    result: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
    for tf, items in by_tf.items():
        items.sort(key=lambda x: x["seconds_to_end"])
        result[tf] = {
            "current": items[0] if len(items) > 0 else None,
            "next": items[1] if len(items) > 1 else None,
        }

    return result
