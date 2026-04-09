from typing import Dict, Optional, Tuple

DEFAULT_TICK_SIZE = 0.01
ARBITRAGE_SUM_ASKS_MAX = 0.99
WATCH_SUM_ASKS_MAX = 1.01
MIN_SUM_BIDS_TO_PLAN = 0.99
MAX_EXIT_GAP_TOTAL_TO_PLAN = 0.03
MIN_PLAN_SECS_NEXT_1 = 360
MIN_PLAN_SECS_NEXT_2 = 600


def classify_signal(metrics: Dict[str, float], stable_count: int, min_stable_snapshots: int = 2) -> str:
    if not metrics or stable_count < min_stable_snapshots:
        return "idle"
    if metrics["sum_asks"] <= ARBITRAGE_SUM_ASKS_MAX:
        return "armed"
    if metrics["sum_asks"] <= WATCH_SUM_ASKS_MAX:
        return "watching"
    return "idle"


def compute_exit_gap_total(metrics: Dict[str, float], tick_size: float = DEFAULT_TICK_SIZE) -> float:
    projected_exit_up = round(float(metrics["up_ask"]) + tick_size, 2)
    projected_exit_down = round(float(metrics["down_ask"]) + tick_size, 2)
    return round((projected_exit_up - float(metrics["up_bid"])) + (projected_exit_down - float(metrics["down_bid"])), 4)


def evaluate_entry_quality(metrics: Optional[Dict[str, float]], slot_name: str, secs_to_end: Optional[int]) -> Tuple[bool, str, Optional[Dict[str, float]]]:
    if not metrics:
        return False, "metrics unavailable", None

    reasons = []
    if secs_to_end is None:
        reasons.append("secs_to_end unavailable")
    elif slot_name == "next_1" and secs_to_end < MIN_PLAN_SECS_NEXT_1:
        reasons.append(f"secs_to_end {secs_to_end} < {MIN_PLAN_SECS_NEXT_1}")
    elif slot_name == "next_2" and secs_to_end < MIN_PLAN_SECS_NEXT_2:
        reasons.append(f"secs_to_end {secs_to_end} < {MIN_PLAN_SECS_NEXT_2}")

    if float(metrics["sum_bids"]) < MIN_SUM_BIDS_TO_PLAN:
        reasons.append(f"sum_bids {metrics['sum_bids']} < {MIN_SUM_BIDS_TO_PLAN}")

    exit_gap_total = compute_exit_gap_total(metrics)
    if exit_gap_total > MAX_EXIT_GAP_TOTAL_TO_PLAN:
        reasons.append(f"exit_gap_total {exit_gap_total} > {MAX_EXIT_GAP_TOTAL_TO_PLAN}")

    details = {
        "sum_asks": float(metrics["sum_asks"]),
        "sum_bids": float(metrics["sum_bids"]),
        "edge_asks": float(metrics["edge_asks"]),
        "edge_bids": float(metrics["edge_bids"]),
        "exit_gap_total": exit_gap_total,
        "slot_name": slot_name,
        "secs_to_end": secs_to_end,
    }
    return len(reasons) == 0, ("ok" if not reasons else "; ".join(reasons)), details


def plan_two_leg_order(metrics: Dict[str, float], min_shares_per_leg: int = 5) -> Dict[str, float]:
    return {
        "up_limit_price": float(metrics["up_ask"]),
        "down_limit_price": float(metrics["down_ask"]),
        "up_qty": min_shares_per_leg,
        "down_qty": min_shares_per_leg,
        "sum_asks": float(metrics["sum_asks"]),
        "edge_asks": float(metrics["edge_asks"]),
    }
