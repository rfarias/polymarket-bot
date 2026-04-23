from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Optional


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _limit_or_default(value: Optional[float], default: float = 999.0) -> float:
    if value is None:
        return float(default)
    return float(value)


@dataclass
class CurrentAlmostResolvedConfigV1:
    min_secs_to_end: int = 15
    max_secs_to_end: int = 80
    min_entry_price: float = 0.90
    max_entry_price: float = 0.98
    fallback_max_entry_price: float = 0.99
    target_exit_price: float = 0.99
    max_exit_distance: float = 0.04
    min_price_to_beat_distance_bps: float = 5.0
    min_price_to_beat_buffer_bps: float = 2.0
    max_reversal_share_of_open_distance: float = 0.6
    min_price_to_beat_distance_usd: float = 40.0
    min_price_to_beat_buffer_usd: float = 15.0
    healthy_pullback_max_usd: float = 12.0
    healthy_pullback_share_of_open_distance: float = 0.35
    strong_distance_relaxed_threshold_usd: float = 60.0
    strong_distance_relaxed_max_exit_distance: float = 0.09
    strong_distance_relaxed_market_range_30s: float = 0.08
    soft_counter_price_alert: float = 0.10
    strong_counter_price_block: float = 0.22
    max_spread: float = 0.015
    max_leader_counter_price: float = 0.06
    min_depth_top3: float = 15.0
    min_leader_edge_vs_counter: float = 0.90
    max_adverse_spot_5s_bps: float = 0.5
    max_adverse_spot_15s_bps: float = 1.0
    max_adverse_market_5s: float = 0.01
    max_adverse_market_15s: float = 0.015
    max_market_range_15s: float = 0.025
    max_market_range_30s: float = 0.035
    fallback_requires_missing_open_reference: bool = True
    fallback_min_leader_edge_vs_counter: float = 0.90
    fallback_max_counter_price: float = 0.08
    fallback_max_adverse_spot_30s_bps: float = 3.5
    fallback_max_market_range_30s: float = 0.03
    target_ticks: int = 1
    stop_ticks: int = 3
    max_hold_secs: int = 8
    paper_profit_take_min_ticks: float = 1.0
    paper_profit_take_late_secs: int = 35
    paper_profit_take_on_reversal_buffer_bps: float = 2.5
    paper_profit_take_on_market_range_30s: float = 0.025
    paper_hold_to_resolution_secs: int = 12
    paper_hold_to_resolution_min_price: float = 0.97
    paper_hold_to_resolution_min_buffer_bps: float = 4.0
    paper_hold_to_resolution_min_open_distance_bps: float = 8.0
    paper_structural_stop_buffer_bps: float = 1.0
    paper_structural_stop_market_range_30s: float = 0.035
    paper_structural_stop_edge_vs_counter: float = 0.80

    def as_dict(self) -> Dict:
        return asdict(self)


def evaluate_current_almost_resolved_v1(
    *,
    snap: Dict,
    secs_to_end: Optional[int],
    reference_signal: Optional[Dict],
    cfg: Optional[CurrentAlmostResolvedConfigV1] = None,
) -> Dict:
    cfg = cfg or CurrentAlmostResolvedConfigV1()
    up = snap.get("up") or {}
    down = snap.get("down") or {}

    up_buy = _safe_float(up.get("executable_buy") or up.get("best_ask"), -1.0)
    up_sell = _safe_float(up.get("executable_sell") or up.get("best_bid"), -1.0)
    down_buy = _safe_float(down.get("executable_buy") or down.get("best_ask"), -1.0)
    down_sell = _safe_float(down.get("executable_sell") or down.get("best_bid"), -1.0)
    up_spread = round(max(0.0, up_buy - up_sell), 6) if up_buy > 0 and up_sell > 0 else None
    down_spread = round(max(0.0, down_buy - down_sell), 6) if down_buy > 0 and down_sell > 0 else None
    up_depth = sum(_safe_float((lvl or {}).get("size")) for lvl in (up.get("top_bids") or [])[:3]) + sum(
        _safe_float((lvl or {}).get("size")) for lvl in (up.get("top_asks") or [])[:3]
    )
    down_depth = sum(_safe_float((lvl or {}).get("size")) for lvl in (down.get("top_bids") or [])[:3]) + sum(
        _safe_float((lvl or {}).get("size")) for lvl in (down.get("top_asks") or [])[:3]
    )

    result = {
        "setup": "almost_resolved",
        "allow": False,
        "side": None,
        "reason": "no_edge",
        "entry_price": None,
        "exit_price": None,
        "up_buy": up_buy,
        "up_sell": up_sell,
        "down_buy": down_buy,
        "down_sell": down_sell,
        "up_spread": up_spread,
        "down_spread": down_spread,
        "up_depth_top3_both_sides": round(up_depth, 6),
        "down_depth_top3_both_sides": round(down_depth, 6),
        "secs_to_end": secs_to_end,
    }

    if secs_to_end is None or secs_to_end < cfg.min_secs_to_end or secs_to_end > cfg.max_secs_to_end:
        result["reason"] = "outside_time_window"
        return result

    context_reason = str((reference_signal or {}).get("reason") or "")
    if up_buy >= 0.9 and down_buy >= 0.9:
        result["reason"] = "invalid_book_both_sides_rich"
        return result

    spot_delta_5s = _safe_float((reference_signal or {}).get("spot_delta_5s_bps"), 0.0)
    spot_delta_15s = _safe_float((reference_signal or {}).get("spot_delta_15s_bps"), 0.0)
    spot_delta_30s = _safe_float((reference_signal or {}).get("spot_delta_30s_bps"), 0.0)
    distance_from_open = _safe_float((reference_signal or {}).get("distance_from_open_bps"), 0.0)
    missing_open_reference = "missing_open_reference_price" in str((reference_signal or {}).get("reason") or "")
    reference_price = _safe_float((reference_signal or {}).get("reference_price"), 0.0)
    opening_reference_price = _safe_float((reference_signal or {}).get("opening_reference_price"), 0.0)
    market_delta_5s = _safe_float((reference_signal or {}).get("market_delta_5s"), 0.0)
    market_delta_15s = _safe_float((reference_signal or {}).get("market_delta_15s"), 0.0)
    market_range_15s = _safe_float((reference_signal or {}).get("market_range_15s"), 0.0)
    market_range_30s = _safe_float((reference_signal or {}).get("market_range_30s"), 0.0)

    up_edge_vs_counter = round(up_buy - down_buy, 6) if up_buy > 0 and down_buy > 0 else None
    down_edge_vs_counter = round(down_buy - up_buy, 6) if up_buy > 0 and down_buy > 0 else None
    up_exit_distance = round(cfg.target_exit_price - up_buy, 6) if up_buy > 0 else None
    down_exit_distance = round(cfg.target_exit_price - down_buy, 6) if down_buy > 0 else None
    distance_to_price_to_beat_bps = round(abs(distance_from_open), 4)
    distance_to_price_to_beat_usd = (
        round(abs(reference_price - opening_reference_price), 4)
        if reference_price > 0 and opening_reference_price > 0
        else 0.0
    )
    up_adverse_spot_bps = round(max(0.0, -spot_delta_5s, -spot_delta_15s, -spot_delta_30s), 4)
    down_adverse_spot_bps = round(max(0.0, spot_delta_5s, spot_delta_15s, spot_delta_30s), 4)
    up_adverse_spot_usd = round(reference_price * up_adverse_spot_bps / 10000.0, 4) if reference_price > 0 else 0.0
    down_adverse_spot_usd = round(reference_price * down_adverse_spot_bps / 10000.0, 4) if reference_price > 0 else 0.0
    up_price_to_beat_buffer_bps = round(distance_to_price_to_beat_bps - up_adverse_spot_bps, 4)
    down_price_to_beat_buffer_bps = round(distance_to_price_to_beat_bps - down_adverse_spot_bps, 4)
    up_price_to_beat_buffer_usd = round(distance_to_price_to_beat_usd - up_adverse_spot_usd, 4)
    down_price_to_beat_buffer_usd = round(distance_to_price_to_beat_usd - down_adverse_spot_usd, 4)
    pullback_usd_cap = max(
        cfg.healthy_pullback_max_usd,
        round(distance_to_price_to_beat_usd * cfg.healthy_pullback_share_of_open_distance, 4),
    )
    result["up_edge_vs_counter"] = up_edge_vs_counter
    result["down_edge_vs_counter"] = down_edge_vs_counter
    result["up_exit_distance"] = up_exit_distance
    result["down_exit_distance"] = down_exit_distance
    result["distance_to_price_to_beat_bps"] = distance_to_price_to_beat_bps
    result["distance_to_price_to_beat_usd"] = distance_to_price_to_beat_usd
    result["up_adverse_spot_bps"] = up_adverse_spot_bps
    result["down_adverse_spot_bps"] = down_adverse_spot_bps
    result["up_adverse_spot_usd"] = up_adverse_spot_usd
    result["down_adverse_spot_usd"] = down_adverse_spot_usd
    result["up_price_to_beat_buffer_bps"] = up_price_to_beat_buffer_bps
    result["down_price_to_beat_buffer_bps"] = down_price_to_beat_buffer_bps
    result["up_price_to_beat_buffer_usd"] = up_price_to_beat_buffer_usd
    result["down_price_to_beat_buffer_usd"] = down_price_to_beat_buffer_usd
    up_counter_pressure_ok = down_buy <= cfg.strong_counter_price_block
    down_counter_pressure_ok = up_buy <= cfg.strong_counter_price_block

    result["pullback_usd_cap"] = pullback_usd_cap
    result["market_range_15s"] = market_range_15s
    result["market_range_30s"] = market_range_30s
    result["missing_open_reference"] = missing_open_reference
    result["missing_market_midpoint_context"] = "missing_market_midpoint" in context_reason
    result["up_counter_pressure_ok"] = up_counter_pressure_ok
    result["down_counter_pressure_ok"] = down_counter_pressure_ok
    result["up_counter_alert"] = down_buy >= cfg.soft_counter_price_alert
    result["down_counter_alert"] = up_buy >= cfg.soft_counter_price_alert

    up_distance_relaxed_ok = (
        distance_to_price_to_beat_usd >= cfg.min_price_to_beat_distance_usd
        and up_price_to_beat_buffer_usd >= cfg.min_price_to_beat_buffer_usd
        and up_adverse_spot_usd <= pullback_usd_cap
        and up_counter_pressure_ok
        and market_range_30s
        <= (
            cfg.strong_distance_relaxed_market_range_30s
            if distance_to_price_to_beat_usd >= cfg.strong_distance_relaxed_threshold_usd
            else cfg.max_market_range_30s
        )
        and _safe_float(up_exit_distance, 999.0)
        <= (
            cfg.strong_distance_relaxed_max_exit_distance
            if distance_to_price_to_beat_usd >= cfg.strong_distance_relaxed_threshold_usd
            else cfg.max_exit_distance
        )
    )
    down_distance_relaxed_ok = (
        distance_to_price_to_beat_usd >= cfg.min_price_to_beat_distance_usd
        and down_price_to_beat_buffer_usd >= cfg.min_price_to_beat_buffer_usd
        and down_adverse_spot_usd <= pullback_usd_cap
        and down_counter_pressure_ok
        and market_range_30s
        <= (
            cfg.strong_distance_relaxed_market_range_30s
            if distance_to_price_to_beat_usd >= cfg.strong_distance_relaxed_threshold_usd
            else cfg.max_market_range_30s
        )
        and _safe_float(down_exit_distance, 999.0)
        <= (
            cfg.strong_distance_relaxed_max_exit_distance
            if distance_to_price_to_beat_usd >= cfg.strong_distance_relaxed_threshold_usd
            else cfg.max_exit_distance
        )
    )

    if (
        cfg.min_entry_price <= up_buy <= cfg.max_entry_price
        and _limit_or_default(up_spread) <= max(cfg.max_spread, 0.02 if distance_to_price_to_beat_usd >= cfg.strong_distance_relaxed_threshold_usd else cfg.max_spread)
        and up_depth >= cfg.min_depth_top3
        and distance_from_open >= cfg.min_price_to_beat_distance_bps
        and up_counter_pressure_ok
        and (
            (
                _safe_float(up_exit_distance, 999.0) <= cfg.max_exit_distance
                and up_adverse_spot_bps <= distance_to_price_to_beat_bps * cfg.max_reversal_share_of_open_distance
                and up_price_to_beat_buffer_bps >= cfg.min_price_to_beat_buffer_bps
                and spot_delta_5s >= -cfg.max_adverse_spot_5s_bps
                and spot_delta_15s >= -cfg.max_adverse_spot_15s_bps
                and market_delta_5s >= -cfg.max_adverse_market_5s
                and market_delta_15s >= -cfg.max_adverse_market_15s
                and market_range_15s <= cfg.max_market_range_15s
                and market_range_30s <= cfg.max_market_range_30s
            )
            or up_distance_relaxed_ok
        )
    ):
        result.update(
            {
                "allow": True,
                "side": "UP",
                "reason": "leader_up_near_resolution_without_reversal",
                "entry_price": up_buy,
                "exit_price": min(cfg.target_exit_price, 0.99),
            }
        )
        return result

    if (
        (not cfg.fallback_requires_missing_open_reference or missing_open_reference)
        and cfg.min_entry_price <= up_buy <= cfg.fallback_max_entry_price
        and _limit_or_default(up_spread) <= max(cfg.max_spread, 0.02)
        and up_depth >= cfg.min_depth_top3
        and up_counter_pressure_ok
        and (
            (
                _safe_float(up_exit_distance, 999.0) <= cfg.max_exit_distance
                and up_adverse_spot_bps <= cfg.fallback_max_adverse_spot_30s_bps
                and spot_delta_15s >= -cfg.max_adverse_spot_15s_bps
                and spot_delta_30s >= -0.5
                and market_range_15s <= cfg.max_market_range_15s
                and market_range_30s <= cfg.fallback_max_market_range_30s
            )
            or up_distance_relaxed_ok
        )
    ):
        result.update(
            {
                "allow": True,
                "side": "UP",
                "reason": "leader_up_fallback_without_open_reference",
                "entry_price": up_buy,
                "exit_price": min(cfg.target_exit_price, 0.99),
            }
        )
        return result

    if (
        cfg.min_entry_price <= down_buy <= cfg.max_entry_price
        and _limit_or_default(down_spread) <= max(cfg.max_spread, 0.02 if distance_to_price_to_beat_usd >= cfg.strong_distance_relaxed_threshold_usd else cfg.max_spread)
        and down_depth >= cfg.min_depth_top3
        and distance_from_open <= -cfg.min_price_to_beat_distance_bps
        and down_counter_pressure_ok
        and (
            (
                _safe_float(down_exit_distance, 999.0) <= cfg.max_exit_distance
                and down_adverse_spot_bps <= distance_to_price_to_beat_bps * cfg.max_reversal_share_of_open_distance
                and down_price_to_beat_buffer_bps >= cfg.min_price_to_beat_buffer_bps
                and spot_delta_5s <= cfg.max_adverse_spot_5s_bps
                and spot_delta_15s <= cfg.max_adverse_spot_15s_bps
                and market_delta_5s <= cfg.max_adverse_market_5s
                and market_delta_15s <= cfg.max_adverse_market_15s
                and market_range_15s <= cfg.max_market_range_15s
                and market_range_30s <= cfg.max_market_range_30s
            )
            or down_distance_relaxed_ok
        )
    ):
        result.update(
            {
                "allow": True,
                "side": "DOWN",
                "reason": "leader_down_near_resolution_without_reversal",
                "entry_price": down_buy,
                "exit_price": min(cfg.target_exit_price, 0.99),
            }
        )
        return result

    if (
        (not cfg.fallback_requires_missing_open_reference or missing_open_reference)
        and cfg.min_entry_price <= down_buy <= cfg.fallback_max_entry_price
        and _limit_or_default(down_spread) <= max(cfg.max_spread, 0.02)
        and down_depth >= cfg.min_depth_top3
        and down_counter_pressure_ok
        and (
            (
                _safe_float(down_exit_distance, 999.0) <= cfg.max_exit_distance
                and down_adverse_spot_bps <= cfg.fallback_max_adverse_spot_30s_bps
                and spot_delta_15s <= cfg.max_adverse_spot_15s_bps
                and spot_delta_30s <= 0.5
                and market_range_15s <= cfg.max_market_range_15s
                and market_range_30s <= cfg.fallback_max_market_range_30s
            )
            or down_distance_relaxed_ok
        )
    ):
        result.update(
            {
                "allow": True,
                "side": "DOWN",
                "reason": "leader_down_fallback_without_open_reference",
                "entry_price": down_buy,
                "exit_price": min(cfg.target_exit_price, 0.99),
            }
        )
        return result

    result["reason"] = "leader_not_stable_enough_or_not_priced_for_ticks"
    return result
