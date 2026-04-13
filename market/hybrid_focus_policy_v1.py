from __future__ import annotations

from typing import Dict, Optional, Tuple

NEXT1_PRIMARY_FOCUS_UNTIL_SECS = 480


def evaluate_hybrid_slot_focus_v1(
    *,
    slot_name: str,
    next_1_secs_to_end: Optional[int],
    next_1_active_plan_id: Optional[str],
    max_active_plans_reached: bool,
    allow_next_2_config: bool,
) -> Tuple[bool, str]:
    if slot_name == "next_1":
        if max_active_plans_reached and next_1_active_plan_id is None:
            return False, "max_active_plans_reached"
        return True, "next_1_primary"

    if slot_name != "next_2":
        return False, "unsupported_slot"

    if not allow_next_2_config:
        return False, "next_2_disabled_by_guard"
    if next_1_active_plan_id is not None:
        return False, "next_1_plan_active"
    if max_active_plans_reached:
        return False, "max_active_plans_reached"
    if next_1_secs_to_end is None:
        return False, "next_1_secs_unavailable"
    if next_1_secs_to_end > NEXT1_PRIMARY_FOCUS_UNTIL_SECS:
        return False, f"next_1_primary_focus_window>{NEXT1_PRIMARY_FOCUS_UNTIL_SECS}s"
    return True, "next_2_enabled_after_next_1_focus_window"


def summarize_focus_v1(
    *,
    next_1_secs_to_end: Optional[int],
    next_1_active_plan_id: Optional[str],
    max_active_plans_reached: bool,
    allow_next_2_config: bool,
) -> Dict[str, Dict[str, object]]:
    summary: Dict[str, Dict[str, object]] = {}
    for slot_name in ("next_1", "next_2"):
        allowed, reason = evaluate_hybrid_slot_focus_v1(
            slot_name=slot_name,
            next_1_secs_to_end=next_1_secs_to_end,
            next_1_active_plan_id=next_1_active_plan_id,
            max_active_plans_reached=max_active_plans_reached,
            allow_next_2_config=allow_next_2_config,
        )
        summary[slot_name] = {"allowed": allowed, "reason": reason}
    return summary
