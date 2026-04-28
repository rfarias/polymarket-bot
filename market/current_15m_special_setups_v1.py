from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Optional


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _sum_depth(levels: Optional[list[dict]]) -> float:
    total = 0.0
    for lvl in levels or []:
        total += _safe_float((lvl or {}).get("size"), 0.0)
    return round(total, 6)


@dataclass
class Current15mSpecialSetupsConfigV1:
    winner_min_secs_to_end: int = 120
    winner_max_secs_to_end: int = 840
    winner_min_trend_distance_bps: float = 6.0
    winner_min_safe_distance_usd: float = 35.0
    winner_min_safe_distance_bps: float = 5.0
    winner_min_distance_vs_recent_vol_mult: float = 1.25
    winner_min_entry_price: float = 0.95
    winner_max_entry_price: float = 0.985
    winner_max_counter_price: float = 0.14
    winner_min_edge_vs_counter: float = 0.80
    winner_min_pullback_spot_5s_bps: float = 0.04
    winner_min_pullback_market_5s: float = 0.002
    winner_max_adverse_spot_15s_bps: float = 1.2
    winner_max_adverse_spot_30s_bps: float = 1.8
    winner_max_market_range_60s: float = 0.12
    winner_min_entry_visible_size: float = 5.0
    winner_entry_touch_polls: int = 2
    winner_entry_min_age_secs: float = 1.5
    winner_resting_min_secs_to_end: int = 300
    winner_resting_entry_discount: float = 0.01
    winner_resting_min_price: float = 0.95
    winner_resting_max_price: float = 0.98
    winner_resting_timeout_secs: float = 45.0
    winner_short_distance_min_usd: float = 40.0
    winner_short_distance_max_usd: float = 60.0
    winner_short_distance_min_bps: float = 5.0
    winner_short_distance_min_edge_vs_counter: float = 0.84
    winner_short_distance_max_counter_price: float = 0.12
    winner_short_distance_pullback_spot_5s_bps: float = 0.01
    winner_short_distance_pullback_market_5s: float = 0.0005
    winner_short_distance_max_adverse_spot_15s_bps: float = 1.4
    winner_short_distance_max_adverse_spot_30s_bps: float = 2.0
    winner_short_distance_max_market_range_60s: float = 0.14
    winner_very_strong_distance_usd: float = 100.0
    winner_very_strong_distance_bps: float = 12.0
    winner_very_strong_min_edge_vs_counter: float = 0.82
    winner_very_strong_max_counter_price: float = 0.16
    winner_very_strong_pullback_spot_5s_bps: float = 0.0
    winner_very_strong_pullback_market_5s: float = 0.0
    winner_very_strong_max_adverse_spot_15s_bps: float = 1.8
    winner_very_strong_max_adverse_spot_30s_bps: float = 2.4
    winner_very_strong_max_market_range_60s: float = 0.18
    winner_extreme_distance_usd: float = 120.0
    winner_extreme_distance_bps: float = 15.0
    winner_extreme_min_edge_vs_counter: float = 0.78
    winner_extreme_max_counter_price: float = 0.20
    winner_extreme_max_adverse_spot_15s_bps: float = 2.2
    winner_extreme_max_adverse_spot_30s_bps: float = 3.0
    winner_extreme_max_market_range_60s: float = 0.22
    winner_strong_trend_distance_bps: float = 8.0
    winner_strong_distance_usd: float = 55.0
    winner_strong_edge_vs_counter: float = 0.88
    winner_strong_pullback_spot_5s_bps: float = 0.02
    winner_strong_pullback_market_5s: float = 0.001
    winner_hold_to_resolution_secs: int = 120
    winner_target_price: float = 0.99
    winner_stop_price: float = 0.96
    winner_max_hold_secs: int = 180
    counter_min_secs_to_end: int = 300
    counter_max_secs_to_end: int = 840
    counter_min_extreme_distance_usd: float = 70.0
    counter_min_extreme_distance_bps: float = 9.0
    counter_min_distance_vs_recent_vol_mult: float = 1.5
    counter_min_winner_price: float = 0.95
    counter_min_entry_price: float = 0.02
    counter_max_entry_price: float = 0.06
    counter_min_bounce_spot_5s_bps: float = 0.08
    counter_min_bounce_market_5s: float = 0.003
    counter_min_entry_visible_size: float = 8.0
    counter_entry_touch_polls: int = 2
    counter_entry_min_age_secs: float = 2.0
    counter_resting_min_secs_to_end: int = 330
    counter_resting_max_loser_price: float = 0.10
    counter_resting_min_extreme_distance_usd: float = 180.0
    counter_resting_min_extreme_distance_bps: float = 20.0
    counter_resting_entry_price_low: float = 0.03
    counter_resting_entry_price_high: float = 0.04
    counter_resting_timeout_secs: float = 90.0
    counter_stop_floor: float = 0.01
    counter_stop_loss_ratio: float = 0.5
    counter_target_profit_ratio: float = 0.5
    counter_target_cap_price: float = 0.12
    counter_structural_resume_winner_price: float = 0.985
    counter_max_hold_secs: int = 150

    def as_dict(self) -> Dict:
        return asdict(self)


def _base_result(setup: str, variant: str, secs_to_end: Optional[int], ref: Dict) -> Dict:
    return {
        "setup": setup,
        "setup_variant": variant,
        "allow": False,
        "side": None,
        "reason": "no_edge",
        "entry_price": None,
        "exit_price": None,
        "stop_price": None,
        "hold_to_resolution": False,
        "secs_to_end": secs_to_end,
        "distance_from_open_bps": _safe_float(ref.get("distance_from_open_bps"), 0.0),
        "spot_delta_5s_bps": _safe_float(ref.get("spot_delta_5s_bps"), 0.0),
        "spot_delta_15s_bps": _safe_float(ref.get("spot_delta_15s_bps"), 0.0),
        "spot_delta_30s_bps": _safe_float(ref.get("spot_delta_30s_bps"), 0.0),
        "market_delta_5s": _safe_float(ref.get("market_delta_5s"), 0.0),
        "market_delta_15s": _safe_float(ref.get("market_delta_15s"), 0.0),
        "market_range_60s": _safe_float(ref.get("market_range_60s"), 0.0),
        "spot_range_60s_usd": _safe_float(ref.get("spot_range_60s_usd"), 0.0),
    }


def evaluate_winner_pullback_15m_v1(
    *,
    snap: Dict,
    secs_to_end: Optional[int],
    reference_signal: Optional[Dict],
    cfg: Optional[Current15mSpecialSetupsConfigV1] = None,
) -> Dict:
    cfg = cfg or Current15mSpecialSetupsConfigV1()
    ref = reference_signal or {}
    result = _base_result("winner_pullback_15m", "winner_pullback", secs_to_end, ref)

    if secs_to_end is None or secs_to_end < cfg.winner_min_secs_to_end or secs_to_end > cfg.winner_max_secs_to_end:
        result["reason"] = "outside_time_window"
        return result

    up = snap.get("up") or {}
    down = snap.get("down") or {}
    up_buy = _safe_float(up.get("executable_buy") or up.get("best_ask"), -1.0)
    up_sell = _safe_float(up.get("executable_sell") or up.get("best_bid"), -1.0)
    down_buy = _safe_float(down.get("executable_buy") or down.get("best_ask"), -1.0)
    down_sell = _safe_float(down.get("executable_sell") or down.get("best_bid"), -1.0)
    up_edge_vs_counter = round(up_buy - down_buy, 6) if up_buy > 0 and down_buy > 0 else 0.0
    down_edge_vs_counter = round(down_buy - up_buy, 6) if up_buy > 0 and down_buy > 0 else 0.0

    distance_from_open_bps = _safe_float(ref.get("distance_from_open_bps"), 0.0)
    reference_price = _safe_float(ref.get("reference_price"), 0.0)
    opening_reference_price = _safe_float(ref.get("opening_reference_price"), 0.0)
    distance_to_price_to_beat_usd = (
        round(abs(reference_price - opening_reference_price), 4)
        if reference_price > 0 and opening_reference_price > 0
        else 0.0
    )
    distance_to_price_to_beat_bps = abs(distance_from_open_bps)
    recent_vol_floor_usd = max(
        _safe_float(ref.get("spot_range_60s_usd"), 0.0),
        abs(reference_price * _safe_float(ref.get("spot_delta_15s_bps"), 0.0) / 10000.0) if reference_price > 0 else 0.0,
        abs(reference_price * _safe_float(ref.get("spot_delta_30s_bps"), 0.0) / 10000.0) if reference_price > 0 else 0.0,
    )
    safe_distance_ok = (
        distance_to_price_to_beat_usd >= cfg.winner_min_safe_distance_usd
        and distance_to_price_to_beat_bps >= cfg.winner_min_safe_distance_bps
        and distance_to_price_to_beat_usd >= recent_vol_floor_usd * cfg.winner_min_distance_vs_recent_vol_mult
    )

    result.update(
        {
            "up_price": up_buy,
            "down_price": down_buy,
            "up_bid": up_sell,
            "down_bid": down_sell,
            "up_edge_vs_counter": up_edge_vs_counter,
            "down_edge_vs_counter": down_edge_vs_counter,
            "distance_to_price_to_beat_usd": distance_to_price_to_beat_usd,
            "distance_to_price_to_beat_bps": distance_to_price_to_beat_bps,
            "recent_vol_floor_usd": round(recent_vol_floor_usd, 4),
            "safe_distance_ok": safe_distance_ok,
            "up_visible_ask_size": _sum_depth((up.get("top_asks") or [])[:2]),
            "down_visible_ask_size": _sum_depth((down.get("top_asks") or [])[:2]),
            "entry_timeout_secs": cfg.winner_resting_timeout_secs,
        }
    )

    if not safe_distance_ok:
        result["reason"] = "distance_not_safe_enough"
        return result

    spot_delta_5s = _safe_float(ref.get("spot_delta_5s_bps"), 0.0)
    spot_delta_15s = _safe_float(ref.get("spot_delta_15s_bps"), 0.0)
    spot_delta_30s = _safe_float(ref.get("spot_delta_30s_bps"), 0.0)
    market_delta_5s = _safe_float(ref.get("market_delta_5s"), 0.0)
    market_range_60s = _safe_float(ref.get("market_range_60s"), 0.0)

    up_pullback_ok = (
        distance_from_open_bps >= cfg.winner_min_trend_distance_bps
        and cfg.winner_min_entry_price <= up_buy <= cfg.winner_max_entry_price
        and down_buy <= cfg.winner_max_counter_price
        and up_edge_vs_counter >= cfg.winner_min_edge_vs_counter
        and (
            spot_delta_5s <= -cfg.winner_min_pullback_spot_5s_bps
            or market_delta_5s <= -cfg.winner_min_pullback_market_5s
        )
        and spot_delta_15s >= -cfg.winner_max_adverse_spot_15s_bps
        and spot_delta_30s >= -cfg.winner_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_max_market_range_60s
    )
    up_short_distance_ok = (
        distance_from_open_bps >= cfg.winner_min_trend_distance_bps
        and cfg.winner_short_distance_min_usd <= distance_to_price_to_beat_usd <= cfg.winner_short_distance_max_usd
        and distance_to_price_to_beat_bps >= cfg.winner_short_distance_min_bps
        and cfg.winner_min_entry_price <= up_buy <= cfg.winner_max_entry_price
        and down_buy <= cfg.winner_short_distance_max_counter_price
        and up_edge_vs_counter >= cfg.winner_short_distance_min_edge_vs_counter
        and (
            spot_delta_5s <= -cfg.winner_short_distance_pullback_spot_5s_bps
            or market_delta_5s <= -cfg.winner_short_distance_pullback_market_5s
            or (spot_delta_5s <= 0 and market_delta_5s <= 0)
        )
        and spot_delta_15s >= -cfg.winner_short_distance_max_adverse_spot_15s_bps
        and spot_delta_30s >= -cfg.winner_short_distance_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_short_distance_max_market_range_60s
    )
    up_very_strong_distance_ok = (
        distance_from_open_bps >= cfg.winner_min_trend_distance_bps
        and distance_to_price_to_beat_usd >= cfg.winner_very_strong_distance_usd
        and distance_to_price_to_beat_bps >= cfg.winner_very_strong_distance_bps
        and cfg.winner_min_entry_price <= up_buy <= cfg.winner_max_entry_price
        and down_buy <= cfg.winner_very_strong_max_counter_price
        and up_edge_vs_counter >= cfg.winner_very_strong_min_edge_vs_counter
        and (
            spot_delta_5s <= -cfg.winner_very_strong_pullback_spot_5s_bps
            or market_delta_5s <= -cfg.winner_very_strong_pullback_market_5s
            or (spot_delta_5s <= 0.1 and market_delta_5s <= 0.01)
        )
        and spot_delta_15s >= -cfg.winner_very_strong_max_adverse_spot_15s_bps
        and spot_delta_30s >= -cfg.winner_very_strong_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_very_strong_max_market_range_60s
    )
    up_extreme_distance_ok = (
        distance_from_open_bps >= cfg.winner_min_trend_distance_bps
        and distance_to_price_to_beat_usd >= cfg.winner_extreme_distance_usd
        and distance_to_price_to_beat_bps >= cfg.winner_extreme_distance_bps
        and cfg.winner_min_entry_price <= up_buy <= cfg.winner_max_entry_price
        and down_buy <= cfg.winner_extreme_max_counter_price
        and up_edge_vs_counter >= cfg.winner_extreme_min_edge_vs_counter
        and spot_delta_15s >= -cfg.winner_extreme_max_adverse_spot_15s_bps
        and spot_delta_30s >= -cfg.winner_extreme_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_extreme_max_market_range_60s
    )
    up_resting_ok = (
        secs_to_end >= cfg.winner_resting_min_secs_to_end
        and distance_from_open_bps >= cfg.winner_min_trend_distance_bps
        and distance_to_price_to_beat_usd >= cfg.winner_very_strong_distance_usd
        and distance_to_price_to_beat_bps >= cfg.winner_very_strong_distance_bps
        and cfg.winner_resting_min_price <= max(cfg.winner_resting_min_price, up_buy - cfg.winner_resting_entry_discount) <= cfg.winner_resting_max_price
        and down_buy <= cfg.winner_very_strong_max_counter_price
        and up_edge_vs_counter >= cfg.winner_very_strong_min_edge_vs_counter
        and spot_delta_15s >= -cfg.winner_very_strong_max_adverse_spot_15s_bps
        and spot_delta_30s >= -cfg.winner_very_strong_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_very_strong_max_market_range_60s
    )
    up_strong_trend_ok = (
        distance_from_open_bps >= cfg.winner_strong_trend_distance_bps
        and distance_to_price_to_beat_usd >= cfg.winner_strong_distance_usd
        and cfg.winner_min_entry_price <= up_buy <= cfg.winner_max_entry_price
        and down_buy <= cfg.winner_max_counter_price
        and up_edge_vs_counter >= cfg.winner_strong_edge_vs_counter
        and (
            spot_delta_5s <= -cfg.winner_strong_pullback_spot_5s_bps
            or market_delta_5s <= -cfg.winner_strong_pullback_market_5s
            or (spot_delta_5s <= 0 and market_delta_5s <= 0)
        )
        and spot_delta_15s >= -cfg.winner_max_adverse_spot_15s_bps
        and spot_delta_30s >= -cfg.winner_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_max_market_range_60s
    )
    down_pullback_ok = (
        distance_from_open_bps <= -cfg.winner_min_trend_distance_bps
        and cfg.winner_min_entry_price <= down_buy <= cfg.winner_max_entry_price
        and up_buy <= cfg.winner_max_counter_price
        and down_edge_vs_counter >= cfg.winner_min_edge_vs_counter
        and (
            spot_delta_5s >= cfg.winner_min_pullback_spot_5s_bps
            or market_delta_5s >= cfg.winner_min_pullback_market_5s
        )
        and spot_delta_15s <= cfg.winner_max_adverse_spot_15s_bps
        and spot_delta_30s <= cfg.winner_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_max_market_range_60s
    )
    down_short_distance_ok = (
        distance_from_open_bps <= -cfg.winner_min_trend_distance_bps
        and cfg.winner_short_distance_min_usd <= distance_to_price_to_beat_usd <= cfg.winner_short_distance_max_usd
        and distance_to_price_to_beat_bps >= cfg.winner_short_distance_min_bps
        and cfg.winner_min_entry_price <= down_buy <= cfg.winner_max_entry_price
        and up_buy <= cfg.winner_short_distance_max_counter_price
        and down_edge_vs_counter >= cfg.winner_short_distance_min_edge_vs_counter
        and (
            spot_delta_5s >= cfg.winner_short_distance_pullback_spot_5s_bps
            or market_delta_5s >= cfg.winner_short_distance_pullback_market_5s
            or (spot_delta_5s >= 0 and market_delta_5s >= 0)
        )
        and spot_delta_15s <= cfg.winner_short_distance_max_adverse_spot_15s_bps
        and spot_delta_30s <= cfg.winner_short_distance_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_short_distance_max_market_range_60s
    )
    down_very_strong_distance_ok = (
        distance_from_open_bps <= -cfg.winner_min_trend_distance_bps
        and distance_to_price_to_beat_usd >= cfg.winner_very_strong_distance_usd
        and distance_to_price_to_beat_bps >= cfg.winner_very_strong_distance_bps
        and cfg.winner_min_entry_price <= down_buy <= cfg.winner_max_entry_price
        and up_buy <= cfg.winner_very_strong_max_counter_price
        and down_edge_vs_counter >= cfg.winner_very_strong_min_edge_vs_counter
        and (
            spot_delta_5s >= cfg.winner_very_strong_pullback_spot_5s_bps
            or market_delta_5s >= cfg.winner_very_strong_pullback_market_5s
            or (spot_delta_5s >= -0.1 and market_delta_5s >= -0.01)
        )
        and spot_delta_15s <= cfg.winner_very_strong_max_adverse_spot_15s_bps
        and spot_delta_30s <= cfg.winner_very_strong_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_very_strong_max_market_range_60s
    )
    down_extreme_distance_ok = (
        distance_from_open_bps <= -cfg.winner_min_trend_distance_bps
        and distance_to_price_to_beat_usd >= cfg.winner_extreme_distance_usd
        and distance_to_price_to_beat_bps >= cfg.winner_extreme_distance_bps
        and cfg.winner_min_entry_price <= down_buy <= cfg.winner_max_entry_price
        and up_buy <= cfg.winner_extreme_max_counter_price
        and down_edge_vs_counter >= cfg.winner_extreme_min_edge_vs_counter
        and spot_delta_15s <= cfg.winner_extreme_max_adverse_spot_15s_bps
        and spot_delta_30s <= cfg.winner_extreme_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_extreme_max_market_range_60s
    )
    down_resting_ok = (
        secs_to_end >= cfg.winner_resting_min_secs_to_end
        and distance_from_open_bps <= -cfg.winner_min_trend_distance_bps
        and distance_to_price_to_beat_usd >= cfg.winner_very_strong_distance_usd
        and distance_to_price_to_beat_bps >= cfg.winner_very_strong_distance_bps
        and cfg.winner_resting_min_price <= max(cfg.winner_resting_min_price, down_buy - cfg.winner_resting_entry_discount) <= cfg.winner_resting_max_price
        and up_buy <= cfg.winner_very_strong_max_counter_price
        and down_edge_vs_counter >= cfg.winner_very_strong_min_edge_vs_counter
        and spot_delta_15s <= cfg.winner_very_strong_max_adverse_spot_15s_bps
        and spot_delta_30s <= cfg.winner_very_strong_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_very_strong_max_market_range_60s
    )
    down_strong_trend_ok = (
        distance_from_open_bps <= -cfg.winner_strong_trend_distance_bps
        and distance_to_price_to_beat_usd >= cfg.winner_strong_distance_usd
        and cfg.winner_min_entry_price <= down_buy <= cfg.winner_max_entry_price
        and up_buy <= cfg.winner_max_counter_price
        and down_edge_vs_counter >= cfg.winner_strong_edge_vs_counter
        and (
            spot_delta_5s >= cfg.winner_strong_pullback_spot_5s_bps
            or market_delta_5s >= cfg.winner_strong_pullback_market_5s
            or (spot_delta_5s >= 0 and market_delta_5s >= 0)
        )
        and spot_delta_15s <= cfg.winner_max_adverse_spot_15s_bps
        and spot_delta_30s <= cfg.winner_max_adverse_spot_30s_bps
        and market_range_60s <= cfg.winner_max_market_range_60s
    )

    if up_pullback_ok or up_short_distance_ok or up_strong_trend_ok or up_very_strong_distance_ok or up_extreme_distance_ok or up_resting_ok:
        result.update(
            {
                "allow": True,
                "side": "UP",
                "reason": (
                    "winner_pullback_up"
                    if up_pullback_ok
                    else (
                        "winner_pullback_up_short_distance"
                        if up_short_distance_ok
                        else (
                            "winner_pullback_up_strong_trend"
                            if up_strong_trend_ok
                            else (
                                "winner_pullback_up_very_strong_distance"
                                if up_very_strong_distance_ok
                                else ("winner_pullback_up_extreme_distance" if up_extreme_distance_ok else "winner_pullback_up_resting")
                            )
                        )
                    )
                ),
                "entry_price": round(max(cfg.winner_resting_min_price, up_buy - cfg.winner_resting_entry_discount), 6) if up_resting_ok else up_buy,
                "exit_price": cfg.winner_target_price,
                "stop_price": cfg.winner_stop_price,
                "hold_to_resolution": secs_to_end <= cfg.winner_hold_to_resolution_secs,
                "entry_touch_polls": cfg.winner_entry_touch_polls,
                "entry_min_age_secs": cfg.winner_entry_min_age_secs,
                "min_visible_entry_size": cfg.winner_min_entry_visible_size,
                "max_hold_secs": cfg.winner_max_hold_secs,
                "entry_timeout_secs": cfg.winner_resting_timeout_secs if up_resting_ok else max(12.0, cfg.winner_resting_timeout_secs * 0.33),
            }
        )
        return result

    if down_pullback_ok or down_short_distance_ok or down_strong_trend_ok or down_very_strong_distance_ok or down_extreme_distance_ok or down_resting_ok:
        result.update(
            {
                "allow": True,
                "side": "DOWN",
                "reason": (
                    "winner_pullback_down"
                    if down_pullback_ok
                    else (
                        "winner_pullback_down_short_distance"
                        if down_short_distance_ok
                        else (
                            "winner_pullback_down_strong_trend"
                            if down_strong_trend_ok
                            else (
                                "winner_pullback_down_very_strong_distance"
                                if down_very_strong_distance_ok
                                else ("winner_pullback_down_extreme_distance" if down_extreme_distance_ok else "winner_pullback_down_resting")
                            )
                        )
                    )
                ),
                "entry_price": round(max(cfg.winner_resting_min_price, down_buy - cfg.winner_resting_entry_discount), 6) if down_resting_ok else down_buy,
                "exit_price": cfg.winner_target_price,
                "stop_price": cfg.winner_stop_price,
                "hold_to_resolution": secs_to_end <= cfg.winner_hold_to_resolution_secs,
                "entry_touch_polls": cfg.winner_entry_touch_polls,
                "entry_min_age_secs": cfg.winner_entry_min_age_secs,
                "min_visible_entry_size": cfg.winner_min_entry_visible_size,
                "max_hold_secs": cfg.winner_max_hold_secs,
                "entry_timeout_secs": cfg.winner_resting_timeout_secs if down_resting_ok else max(12.0, cfg.winner_resting_timeout_secs * 0.33),
            }
        )
        return result

    result["reason"] = "winner_pullback_not_confirmed"
    return result


def evaluate_counter_reversal_15m_v1(
    *,
    snap: Dict,
    secs_to_end: Optional[int],
    reference_signal: Optional[Dict],
    cfg: Optional[Current15mSpecialSetupsConfigV1] = None,
) -> Dict:
    cfg = cfg or Current15mSpecialSetupsConfigV1()
    ref = reference_signal or {}
    result = _base_result("counter_reversal_15m", "counter_reversal", secs_to_end, ref)

    if secs_to_end is None or secs_to_end < cfg.counter_min_secs_to_end or secs_to_end > cfg.counter_max_secs_to_end:
        result["reason"] = "outside_time_window"
        return result

    up = snap.get("up") or {}
    down = snap.get("down") or {}
    up_buy = _safe_float(up.get("executable_buy") or up.get("best_ask"), -1.0)
    up_sell = _safe_float(up.get("executable_sell") or up.get("best_bid"), -1.0)
    down_buy = _safe_float(down.get("executable_buy") or down.get("best_ask"), -1.0)
    down_sell = _safe_float(down.get("executable_sell") or down.get("best_bid"), -1.0)

    distance_from_open_bps = _safe_float(ref.get("distance_from_open_bps"), 0.0)
    reference_price = _safe_float(ref.get("reference_price"), 0.0)
    opening_reference_price = _safe_float(ref.get("opening_reference_price"), 0.0)
    distance_to_price_to_beat_usd = (
        round(abs(reference_price - opening_reference_price), 4)
        if reference_price > 0 and opening_reference_price > 0
        else 0.0
    )
    distance_to_price_to_beat_bps = abs(distance_from_open_bps)
    recent_vol_floor_usd = max(
        _safe_float(ref.get("spot_range_60s_usd"), 0.0),
        abs(reference_price * _safe_float(ref.get("spot_delta_15s_bps"), 0.0) / 10000.0) if reference_price > 0 else 0.0,
        abs(reference_price * _safe_float(ref.get("spot_delta_30s_bps"), 0.0) / 10000.0) if reference_price > 0 else 0.0,
    )
    safe_distance_ok = (
        distance_to_price_to_beat_usd >= cfg.counter_min_extreme_distance_usd
        and distance_to_price_to_beat_bps >= cfg.counter_min_extreme_distance_bps
        and distance_to_price_to_beat_usd >= recent_vol_floor_usd * cfg.counter_min_distance_vs_recent_vol_mult
    )

    result.update(
        {
            "up_price": up_buy,
            "down_price": down_buy,
            "up_bid": up_sell,
            "down_bid": down_sell,
            "distance_to_price_to_beat_usd": distance_to_price_to_beat_usd,
            "distance_to_price_to_beat_bps": distance_to_price_to_beat_bps,
            "recent_vol_floor_usd": round(recent_vol_floor_usd, 4),
            "safe_distance_ok": safe_distance_ok,
            "up_visible_ask_size": _sum_depth((up.get("top_asks") or [])[:3]),
            "down_visible_ask_size": _sum_depth((down.get("top_asks") or [])[:3]),
            "entry_timeout_secs": cfg.counter_resting_timeout_secs,
        }
    )

    if not safe_distance_ok:
        result["reason"] = "distance_not_extreme_enough"
        return result

    spot_delta_5s = _safe_float(ref.get("spot_delta_5s_bps"), 0.0)
    market_delta_5s = _safe_float(ref.get("market_delta_5s"), 0.0)

    buy_down_ok = (
        distance_from_open_bps >= cfg.counter_min_extreme_distance_bps
        and up_buy >= cfg.counter_min_winner_price
        and cfg.counter_min_entry_price <= down_buy <= cfg.counter_max_entry_price
        and (
            spot_delta_5s <= -cfg.counter_min_bounce_spot_5s_bps
            or market_delta_5s <= -cfg.counter_min_bounce_market_5s
        )
    )
    buy_up_ok = (
        distance_from_open_bps <= -cfg.counter_min_extreme_distance_bps
        and down_buy >= cfg.counter_min_winner_price
        and cfg.counter_min_entry_price <= up_buy <= cfg.counter_max_entry_price
        and (
            spot_delta_5s >= cfg.counter_min_bounce_spot_5s_bps
            or market_delta_5s >= cfg.counter_min_bounce_market_5s
        )
    )
    rest_buy_down_ok = (
        secs_to_end >= cfg.counter_resting_min_secs_to_end
        and distance_from_open_bps >= cfg.counter_resting_min_extreme_distance_bps
        and distance_to_price_to_beat_usd >= cfg.counter_resting_min_extreme_distance_usd
        and up_buy >= cfg.counter_min_winner_price
        and down_buy <= cfg.counter_resting_max_loser_price
    )
    rest_buy_up_ok = (
        secs_to_end >= cfg.counter_resting_min_secs_to_end
        and distance_from_open_bps <= -cfg.counter_resting_min_extreme_distance_bps
        and distance_to_price_to_beat_usd >= cfg.counter_resting_min_extreme_distance_usd
        and down_buy >= cfg.counter_min_winner_price
        and up_buy <= cfg.counter_resting_max_loser_price
    )

    if buy_down_ok:
        entry = down_buy
        target = min(cfg.counter_target_cap_price, round(max(entry + 0.01, entry * (1.0 + cfg.counter_target_profit_ratio)), 6))
        stop = max(cfg.counter_stop_floor, round(entry * cfg.counter_stop_loss_ratio, 6))
        result.update(
            {
                "allow": True,
                "side": "DOWN",
                "reason": "counter_reversal_down_after_up_extension",
                "entry_price": entry,
                "exit_price": target,
                "stop_price": stop,
                "entry_touch_polls": cfg.counter_entry_touch_polls,
                "entry_min_age_secs": cfg.counter_entry_min_age_secs,
                "min_visible_entry_size": cfg.counter_min_entry_visible_size,
                "max_hold_secs": cfg.counter_max_hold_secs,
                "resume_winner_price": cfg.counter_structural_resume_winner_price,
                "breakeven_on_invalidation": True,
            }
        )
        return result

    if buy_up_ok:
        entry = up_buy
        target = min(cfg.counter_target_cap_price, round(max(entry + 0.01, entry * (1.0 + cfg.counter_target_profit_ratio)), 6))
        stop = max(cfg.counter_stop_floor, round(entry * cfg.counter_stop_loss_ratio, 6))
        result.update(
            {
                "allow": True,
                "side": "UP",
                "reason": "counter_reversal_up_after_down_extension",
                "entry_price": entry,
                "exit_price": target,
                "stop_price": stop,
                "entry_touch_polls": cfg.counter_entry_touch_polls,
                "entry_min_age_secs": cfg.counter_entry_min_age_secs,
                "min_visible_entry_size": cfg.counter_min_entry_visible_size,
                "max_hold_secs": cfg.counter_max_hold_secs,
                "resume_winner_price": cfg.counter_structural_resume_winner_price,
                "breakeven_on_invalidation": True,
            }
        )
        return result

    if rest_buy_down_ok:
        result.update(
            {
                "allow": True,
                "side": "DOWN",
                "reason": "counter_reversal_down_resting",
                "entry_price": cfg.counter_resting_entry_price_low if down_buy <= 0.07 else cfg.counter_resting_entry_price_high,
                "exit_price": min(cfg.counter_target_cap_price, 0.06),
                "stop_price": max(cfg.counter_stop_floor, round((cfg.counter_resting_entry_price_low if down_buy <= 0.07 else cfg.counter_resting_entry_price_high) * cfg.counter_stop_loss_ratio, 6)),
                "entry_touch_polls": cfg.counter_entry_touch_polls,
                "entry_min_age_secs": cfg.counter_entry_min_age_secs,
                "min_visible_entry_size": cfg.counter_min_entry_visible_size,
                "max_hold_secs": cfg.counter_max_hold_secs,
                "resume_winner_price": cfg.counter_structural_resume_winner_price,
                "breakeven_on_invalidation": True,
                "entry_timeout_secs": cfg.counter_resting_timeout_secs,
            }
        )
        return result

    if rest_buy_up_ok:
        result.update(
            {
                "allow": True,
                "side": "UP",
                "reason": "counter_reversal_up_resting",
                "entry_price": cfg.counter_resting_entry_price_low if up_buy <= 0.07 else cfg.counter_resting_entry_price_high,
                "exit_price": min(cfg.counter_target_cap_price, 0.06),
                "stop_price": max(cfg.counter_stop_floor, round((cfg.counter_resting_entry_price_low if up_buy <= 0.07 else cfg.counter_resting_entry_price_high) * cfg.counter_stop_loss_ratio, 6)),
                "entry_touch_polls": cfg.counter_entry_touch_polls,
                "entry_min_age_secs": cfg.counter_entry_min_age_secs,
                "min_visible_entry_size": cfg.counter_min_entry_visible_size,
                "max_hold_secs": cfg.counter_max_hold_secs,
                "resume_winner_price": cfg.counter_structural_resume_winner_price,
                "breakeven_on_invalidation": True,
                "entry_timeout_secs": cfg.counter_resting_timeout_secs,
            }
        )
        return result

    result["reason"] = "counter_reversal_not_confirmed"
    return result
