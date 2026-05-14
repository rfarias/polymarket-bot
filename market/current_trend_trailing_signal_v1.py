from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class CurrentTrendTrailingConfigV1:
    min_secs_to_end: int = 20
    max_secs_to_end: int = 170
    min_abs_distance_bps: float = 6.0
    min_spot_delta_15s_bps: float = 1.0
    min_market_delta_15s: float = 0.0
    min_entry_price: float = 0.65
    max_entry_price: float = 0.88
    max_spread: float = 0.02
    max_source_divergence_bps: float = 6.0
    min_combined_depth_top3: float = 10.0
    initial_stop_ticks: int = 4
    trailing_stop_ticks: int = 3
    arm_after_ticks: int = 2
    target_price: float = 0.98

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _base_result(scalp: dict[str, Any], cfg: CurrentTrendTrailingConfigV1) -> dict[str, Any]:
    return {
        "setup": "current_trend_trailing_v1",
        "variant": "scenario_2",
        "allow": False,
        "side": None,
        "reason": "not_evaluated",
        "entry_price": None,
        "exit_price": None,
        "stop_price": None,
        "target_price": cfg.target_price,
        "initial_stop_ticks": cfg.initial_stop_ticks,
        "trailing_stop_ticks": cfg.trailing_stop_ticks,
        "arm_after_ticks": cfg.arm_after_ticks,
        "secs_to_end": scalp.get("secs_to_end"),
        "distance_from_open_bps": scalp.get("distance_from_open_bps"),
        "spot_delta_15s_bps": scalp.get("spot_delta_15s_bps"),
        "market_delta_15s": scalp.get("market_delta_15s"),
        "up_bid": scalp.get("up_bid"),
        "up_ask": scalp.get("up_ask"),
        "down_bid": scalp.get("down_bid"),
        "down_ask": scalp.get("down_ask"),
        "spread_up": scalp.get("spread_up"),
        "spread_down": scalp.get("spread_down"),
        "source_divergence_bps": scalp.get("source_divergence_bps"),
        "combined_depth_top3": scalp.get("combined_depth_top3"),
    }


def evaluate_current_trend_trailing_v1(
    scalp: dict[str, Any] | None,
    cfg: CurrentTrendTrailingConfigV1 | None = None,
) -> dict[str, Any]:
    cfg = cfg or CurrentTrendTrailingConfigV1()
    scalp = scalp or {}
    result = _base_result(scalp, cfg)

    reasons: list[str] = []
    secs = _safe_float(scalp.get("secs_to_end"), -1.0)
    distance = _safe_float(scalp.get("distance_from_open_bps"), 0.0)
    spot15 = _safe_float(scalp.get("spot_delta_15s_bps"), 0.0)
    market15 = _safe_float(scalp.get("market_delta_15s"), 0.0)
    source_div = _safe_float(scalp.get("source_divergence_bps"), 0.0)
    depth = _safe_float(scalp.get("combined_depth_top3"), 0.0)

    if secs < cfg.min_secs_to_end or secs > cfg.max_secs_to_end:
        reasons.append("outside_time_window")
    if abs(distance) < cfg.min_abs_distance_bps:
        reasons.append("distance_not_strong_enough")
    if source_div > cfg.max_source_divergence_bps:
        reasons.append("source_divergence_too_high")
    if depth < cfg.min_combined_depth_top3:
        reasons.append("low_combined_depth")

    if distance > 0:
        side = "UP"
        bid = _safe_float(scalp.get("up_bid"), 0.0)
        ask = _safe_float(scalp.get("up_ask"), 0.0)
        spread = _safe_float(scalp.get("spread_up"), max(0.0, ask - bid))
        directional_market15 = market15
        directional_spot15 = spot15
    elif distance < 0:
        side = "DOWN"
        bid = _safe_float(scalp.get("down_bid"), 0.0)
        ask = _safe_float(scalp.get("down_ask"), 0.0)
        spread = _safe_float(scalp.get("spread_down"), max(0.0, ask - bid))
        directional_market15 = -market15
        directional_spot15 = -spot15
    else:
        side = None
        bid = ask = spread = 0.0
        directional_market15 = 0.0
        directional_spot15 = 0.0

    if not side:
        reasons.append("missing_direction")
    if directional_spot15 < cfg.min_spot_delta_15s_bps:
        reasons.append("spot_15s_not_confirming")
    if directional_market15 < cfg.min_market_delta_15s:
        reasons.append("market_15s_not_confirming")
    if ask < cfg.min_entry_price or ask > cfg.max_entry_price:
        reasons.append("entry_price_outside_band")
    if spread <= 0 or spread > cfg.max_spread:
        reasons.append("spread_too_wide")

    result.update(
        {
            "side": side,
            "entry_price": round(ask, 6) if ask > 0 else None,
            "exit_price": round(bid, 6) if bid > 0 else None,
            "stop_price": round(max(0.01, ask - cfg.initial_stop_ticks * 0.01), 6) if ask > 0 else None,
            "directional_spot_delta_15s_bps": directional_spot15,
            "directional_market_delta_15s": directional_market15,
            "spread": spread,
        }
    )

    if reasons:
        result["reason"] = "|".join(reasons)
        return result

    result["allow"] = True
    result["reason"] = "trend_confirmed_scenario_2"
    return result
