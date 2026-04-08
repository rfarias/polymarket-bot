from typing import Dict, Any, List, Optional

from market.slug_discovery_v2 import fetch_operational_fast_events

TIMEFRAME_SECONDS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}

CURRENT_HORIZON = {
    "5m": 20 * 60,
    "15m": 45 * 60,
    "1h": 2 * 3600,
}

NEXT_HORIZON = {
    "5m": 40 * 60,
    "15m": 60 * 60,
    "1h": 3 * 3600,
}

MAX_GAP_FACTOR = 2.5
FIVE_MINUTE_ROLL_QUEUE_SIZE = 3  # current + next_1 + next_2
FIVE_MINUTE_UNFILLED_EXIT_TRIGGER_SECS_TO_END = 330  # 5m30s para o mercado next


def _group_by_tf(events: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {"5m": [], "15m": [], "1h": []}
    for ev in events:
        tf = ev.get("timeframe")
        if tf in grouped:
            grouped[tf].append(ev)

    for items in grouped.values():
        items.sort(key=lambda x: x["seconds_to_end"])

    return grouped


def _is_current_candidate(item: Dict[str, Any], tf: str) -> bool:
    return item["seconds_to_end"] <= CURRENT_HORIZON[tf]


def _is_next_candidate(item: Dict[str, Any], tf: str) -> bool:
    return item["seconds_to_end"] <= NEXT_HORIZON[tf]


def _sequence_after(base: Dict[str, Any], items: List[Dict[str, Any]], tf: str, count: int) -> List[Dict[str, Any]]:
    tf_secs = TIMEFRAME_SECONDS[tf]
    picked: List[Dict[str, Any]] = []
    last = base

    for item in items:
        if item["slug"] == base["slug"]:
            continue
        if any(p["slug"] == item["slug"] for p in picked):
            continue
        if not _is_next_candidate(item, tf):
            continue

        gap = item["seconds_to_end"] - last["seconds_to_end"]
        if 0 < gap <= tf_secs * MAX_GAP_FACTOR:
            picked.append(item)
            last = item
            if len(picked) >= count:
                break

    return picked


def build_operational_slots(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
    grouped = _group_by_tf(events)

    result: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {
        "5m": {"current": None, "next_1": None, "next_2": None},
        "15m": {"current": None, "next": None},
        "1h": {"current": None, "next": None},
    }

    # 5m: current + next_1 + next_2
    items_5m = grouped["5m"]
    current_5m = next((x for x in items_5m if _is_current_candidate(x, "5m")), None)
    if current_5m:
        seq = _sequence_after(current_5m, items_5m, "5m", count=2)
        result["5m"] = {
            "current": current_5m,
            "next_1": seq[0] if len(seq) > 0 else None,
            "next_2": seq[1] if len(seq) > 1 else None,
            "unfilled_exit_trigger_secs_to_end_on_next_1": FIVE_MINUTE_UNFILLED_EXIT_TRIGGER_SECS_TO_END,
        }
    else:
        # fallback: se não houver current, ainda assim tenta montar fila dos próximos dois
        upcoming = [x for x in items_5m if _is_next_candidate(x, "5m")]
        result["5m"] = {
            "current": None,
            "next_1": upcoming[0] if len(upcoming) > 0 else None,
            "next_2": upcoming[1] if len(upcoming) > 1 else None,
            "unfilled_exit_trigger_secs_to_end_on_next_1": FIVE_MINUTE_UNFILLED_EXIT_TRIGGER_SECS_TO_END,
        }

    # 15m e 1h: current + next
    for tf in ("15m", "1h"):
        items = grouped[tf]
        current = next((x for x in items if _is_current_candidate(x, tf)), None)
        if current:
            seq = _sequence_after(current, items, tf, count=1)
            result[tf] = {
                "current": current,
                "next": seq[0] if len(seq) > 0 else None,
            }
        else:
            upcoming = [x for x in items if _is_next_candidate(x, tf)]
            result[tf] = {
                "current": None,
                "next": upcoming[0] if len(upcoming) > 0 else None,
            }

    return result


def fetch_operational_slots_v2() -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
    events = fetch_operational_fast_events()
    slots = build_operational_slots(events)
    print("[OPERATIONAL] Current/next queue ready")
    return slots
