from typing import List, Dict, Any, Optional

from market.slug_discovery_v2 import fetch_operational_fast_events

TIMEFRAME_SECONDS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}

# horizonte máximo para considerar que existe um mercado 'atual' utilizável
CURRENT_HORIZON = {
    "5m": 20 * 60,
    "15m": 45 * 60,
    "1h": 2 * 3600,
}

# horizonte máximo para considerar a 'próxima' janela útil
NEXT_HORIZON = {
    "5m": 30 * 60,
    "15m": 60 * 60,
    "1h": 3 * 3600,
}

# gap máximo entre current e next para tratar como janelas consecutivas
MAX_GAP_FACTOR = 2.5


def _pick_current(items: List[Dict[str, Any]], tf: str) -> Optional[Dict[str, Any]]:
    if not items:
        return None
    candidate = items[0]
    if candidate["seconds_to_end"] <= CURRENT_HORIZON[tf]:
        return candidate
    return None


def _pick_next(items: List[Dict[str, Any]], tf: str, current: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not current or len(items) < 2:
        return None

    tf_secs = TIMEFRAME_SECONDS[tf]
    current_end = current["seconds_to_end"]

    for candidate in items[1:]:
        if candidate["seconds_to_end"] > NEXT_HORIZON[tf]:
            continue
        gap = candidate["seconds_to_end"] - current_end
        if 0 < gap <= tf_secs * MAX_GAP_FACTOR:
            return candidate
    return None


def split_current_and_next(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
    by_tf: Dict[str, List[Dict[str, Any]]] = {"5m": [], "15m": [], "1h": []}

    for ev in events:
        tf = ev["timeframe"]
        if tf in by_tf:
            by_tf[tf].append(ev)

    result: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
    for tf, items in by_tf.items():
        items.sort(key=lambda x: x["seconds_to_end"])
        current = _pick_current(items, tf)
        nxt = _pick_next(items, tf, current)
        result[tf] = {
            "current": current,
            "next": nxt,
        }

    return result


def fetch_operational_slots() -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
    events = fetch_operational_fast_events()
    slots = split_current_and_next(events)
    print("[OPERATIONAL] Current/next slots ready")
    return slots
