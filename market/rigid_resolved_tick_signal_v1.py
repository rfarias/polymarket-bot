from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Optional


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _round_price(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 6)


def _first_level_price(book: Dict, side: str, default: float = -1.0) -> float:
    levels = book.get(side) or []
    if not levels:
        return float(default)
    return _safe_float((levels[0] or {}).get("price"), default)


def _book_buy_price(book: Dict) -> float:
    executable_buy = _safe_float(book.get("executable_buy"), -1.0)
    if executable_buy > 0:
        return executable_buy
    best_ask = _safe_float(book.get("best_ask"), -1.0)
    if best_ask > 0:
        return best_ask
    return _first_level_price(book, "top_asks", -1.0)


def _book_sell_price(book: Dict) -> float:
    executable_sell = _safe_float(book.get("executable_sell"), -1.0)
    if executable_sell > 0:
        return executable_sell
    best_bid = _safe_float(book.get("best_bid"), -1.0)
    if best_bid > 0:
        return best_bid
    return _first_level_price(book, "top_bids", -1.0)


def _one_cent_below(value: float) -> float:
    cents_price = int(max(0.0, min(1.0, float(value))) * 100.0) / 100.0
    return _round_price(cents_price - 0.01)


@dataclass
class RigidResolvedTickConfigV1:
    min_secs_to_end_5m: int = 15
    max_secs_to_end_5m: int = 45
    min_secs_to_end_15m: int = 30
    max_secs_to_end_15m_full: int = 90
    max_secs_to_end_15m_small: int = 120
    min_leader_price: float = 0.97
    preferred_leader_price: float = 0.99
    chase_leader_price: float = 1.0
    max_counter_price: float = 0.03
    min_distance_bps: float = 8.0
    min_distance_usd: float = 50.0
    min_distance_vs_recent_range_mult_5m: float = 3.0
    min_distance_vs_recent_range_mult_15m: float = 4.0
    mismatch_min_distance_usd: float = 120.0
    mismatch_min_distance_vs_recent_range_mult: float = 5.0
    max_adverse_spot_5s_bps: float = 0.35
    max_adverse_spot_15s_bps: float = 0.80
    max_market_range_30s: float = 0.035
    max_spread: float = 0.03
    max_source_divergence_bps: float = 6.0
    min_depth_top3: float = 50.0
    min_entry_score: int = 85
    observe_score: int = 70
    tick_size_default: float = 0.01
    cancel_if_leader_below: float = 0.98
    stop_price: float = 0.96
    target_price: float = 0.99
    allow_97_entry: bool = True

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class RigidResolvedTickStateV1:
    slot_key: str = ""
    side: Optional[str] = None
    phase: str = "watch"  # watch | armed | order | chase
    armed_after_price: Optional[float] = None
    order_price: Optional[float] = None
    last_leader_price: Optional[float] = None
    last_reason: str = "not_initialized"

    def reset(self, slot_key: str = "") -> None:
        self.slot_key = slot_key
        self.side = None
        self.phase = "watch"
        self.armed_after_price = None
        self.order_price = None
        self.last_leader_price = None
        self.last_reason = "reset"

    def as_dict(self) -> Dict:
        return asdict(self)


def _side_metrics(snap: Dict, side: str) -> Dict:
    book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    counter = (snap.get("down") if side == "UP" else snap.get("up")) or {}
    leader_ask = _book_buy_price(book)
    leader_bid = _book_sell_price(book)
    leader_price = leader_ask if leader_ask > 0 else _safe_float(book.get("display_price"), leader_bid)
    counter_ask = _book_buy_price(counter)
    counter_bid = _book_sell_price(counter)
    counter_price = counter_ask if counter_ask > 0 else _round_price(1.0 - leader_price)
    tick_size = max(0.001, _safe_float(book.get("tick_size"), 0.01))
    leader_depth_top3 = sum(_safe_float((lvl or {}).get("size")) for lvl in (book.get("top_bids") or [])[:3]) + sum(
        _safe_float((lvl or {}).get("size")) for lvl in (book.get("top_asks") or [])[:3]
    )
    leader_spread = round(max(0.0, leader_ask - leader_bid), 6) if leader_ask > 0 and leader_bid > 0 else None
    return {
        "leader_buy": leader_price,
        "leader_ask": leader_ask,
        "leader_sell": leader_bid,
        "counter_buy": counter_price,
        "counter_ask": counter_ask,
        "counter_bid": counter_bid,
        "tick_size": tick_size,
        "leader_depth_top3": round(leader_depth_top3, 6),
        "leader_spread": leader_spread,
    }


def _reference_side(reference_signal: Optional[Dict]) -> Optional[str]:
    distance = _safe_float((reference_signal or {}).get("distance_from_open_bps"), 0.0)
    if distance > 0:
        return "UP"
    if distance < 0:
        return "DOWN"
    return None


def _book_price_for_side(snap: Dict, side: str) -> float:
    book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    bid = _safe_float(book.get("best_bid") or book.get("executable_sell"), -1.0)
    display = _safe_float(book.get("display_price"), -1.0)
    ask = _safe_float(book.get("best_ask") or book.get("executable_buy"), -1.0)
    if bid > 0:
        return bid
    if display > 0:
        return display
    return ask


def _candidate_side(snap: Dict, reference_signal: Optional[Dict]) -> Optional[str]:
    return _reference_side(reference_signal)


def _book_dominant_side(snap: Dict) -> Optional[str]:
    up_price = _book_price_for_side(snap, "UP")
    down_price = _book_price_for_side(snap, "DOWN")
    if up_price <= 0 and down_price <= 0:
        return None
    return "UP" if up_price >= down_price else "DOWN"


def _context_ok(
    *,
    side: str,
    timeframe: str,
    secs_to_end: Optional[int],
    reference_signal: Optional[Dict],
    metrics: Dict,
    book_side: Optional[str],
    cfg: RigidResolvedTickConfigV1,
) -> tuple[bool, str, Dict]:
    if secs_to_end is None or secs_to_end <= 0:
        return False, "missing_or_expired_time", {}

    if timeframe == "5m" and secs_to_end < cfg.min_secs_to_end_5m:
        return False, "inside_last_seconds_no_entry", {}
    if timeframe == "5m" and secs_to_end > cfg.max_secs_to_end_5m:
        return False, "outside_5m_rigid_time_window", {}
    if timeframe == "15m" and secs_to_end < cfg.min_secs_to_end_15m:
        return False, "inside_last_seconds_no_entry", {}
    if timeframe == "15m" and secs_to_end > cfg.max_secs_to_end_15m_small:
        return False, "outside_15m_small_hand_time_window", {}

    distance_bps_signed = _safe_float((reference_signal or {}).get("distance_from_open_bps"), 0.0)
    distance_bps = abs(distance_bps_signed)
    reference_price = _safe_float((reference_signal or {}).get("reference_price"), 0.0)
    open_price = _safe_float((reference_signal or {}).get("opening_reference_price"), 0.0)
    distance_usd = abs(reference_price - open_price) if reference_price > 0 and open_price > 0 else 0.0
    up_distance_usd = reference_price - open_price if reference_price > 0 and open_price > 0 else 0.0
    down_distance_usd = open_price - reference_price if reference_price > 0 and open_price > 0 else 0.0
    recent_range_usd = _safe_float((reference_signal or {}).get("spot_range_60s_usd"), 0.0)
    market_range_30s = _safe_float((reference_signal or {}).get("market_range_30s"), 0.0)
    spot_delta_5s = _safe_float((reference_signal or {}).get("spot_delta_5s_bps"), 0.0)
    spot_delta_15s = _safe_float((reference_signal or {}).get("spot_delta_15s_bps"), 0.0)
    source_divergence_bps = _safe_float((reference_signal or {}).get("source_divergence_bps"), 0.0)
    side_sign = 1.0 if side == "UP" else -1.0
    adverse_5s = max(0.0, -side_sign * spot_delta_5s)
    adverse_15s = max(0.0, -side_sign * spot_delta_15s)
    distance_vs_recent_range = distance_usd / recent_range_usd if recent_range_usd > 0 else None
    size_label = "full" if timeframe == "5m" or secs_to_end <= cfg.max_secs_to_end_15m_full else "small"

    details = {
        "distance_bps": round(distance_bps, 4),
        "distance_usd": round(distance_usd, 4),
        "current_price": reference_price,
        "price_to_beat": open_price,
        "up_distance_usd": round(up_distance_usd, 4),
        "down_distance_usd": round(down_distance_usd, 4),
        "recent_range_usd": round(recent_range_usd, 4),
        "distance_vs_recent_range": round(distance_vs_recent_range, 4) if distance_vs_recent_range is not None else None,
        "market_range_30s": market_range_30s,
        "adverse_5s_bps": round(adverse_5s, 4),
        "adverse_15s_bps": round(adverse_15s, 4),
        "source_divergence_bps": source_divergence_bps,
        "leader_depth_top3": metrics.get("leader_depth_top3"),
        "leader_spread": metrics.get("leader_spread"),
        "size_label": size_label,
        "book_side": book_side,
        "book_target_side_mismatch": bool(book_side in ("UP", "DOWN") and book_side != side),
    }

    reference_sign = 1.0 if distance_bps_signed > 0 else -1.0 if distance_bps_signed < 0 else side_sign
    adverse_5s = max(0.0, -reference_sign * spot_delta_5s)
    adverse_15s = max(0.0, -reference_sign * spot_delta_15s)
    details["adverse_5s_bps"] = round(adverse_5s, 4)
    details["adverse_15s_bps"] = round(adverse_15s, 4)
    if distance_bps < cfg.min_distance_bps:
        return False, "distance_bps_too_small", details
    if distance_usd < cfg.min_distance_usd:
        return False, "distance_usd_too_small", details
    required_vol_mult = (
        cfg.min_distance_vs_recent_range_mult_15m
        if timeframe == "15m"
        else cfg.min_distance_vs_recent_range_mult_5m
    )
    details["required_distance_vs_recent_range"] = required_vol_mult
    if recent_range_usd > 0 and distance_usd < recent_range_usd * required_vol_mult:
        return False, "distance_not_larger_than_recent_range", details
    if details["book_target_side_mismatch"]:
        mismatch_required_mult = max(required_vol_mult, cfg.mismatch_min_distance_vs_recent_range_mult)
        details["mismatch_required_distance_vs_recent_range"] = mismatch_required_mult
        mismatch_distance_ok = distance_usd >= cfg.mismatch_min_distance_usd
        mismatch_vol_ok = recent_range_usd <= 0 or distance_usd >= recent_range_usd * mismatch_required_mult
        if not mismatch_distance_ok or not mismatch_vol_ok:
            return False, "book_target_side_mismatch_possible_reversal", details
    if metrics["leader_buy"] < cfg.min_leader_price:
        return False, "leader_not_priced_resolved_enough", details
    if metrics["leader_buy"] < 0.98 and not cfg.allow_97_entry:
        return False, "leader_97_entry_disabled", details
    if metrics["counter_buy"] > cfg.max_counter_price:
        return False, "counter_side_too_expensive", details
    if _safe_float(metrics.get("leader_spread"), 999.0) > cfg.max_spread:
        return False, "leader_spread_too_wide", details
    if source_divergence_bps > cfg.max_source_divergence_bps:
        return False, "reference_source_divergence_too_high", details
    if _safe_float(metrics.get("leader_depth_top3"), 0.0) < cfg.min_depth_top3:
        return False, "book_liquidity_too_thin", details
    if adverse_5s > cfg.max_adverse_spot_5s_bps:
        return False, "spot_5s_reversing_against_side", details
    if adverse_15s > cfg.max_adverse_spot_15s_bps:
        return False, "spot_15s_reversing_against_side", details
    if market_range_30s > cfg.max_market_range_30s:
        return False, "market_range_too_wide", details
    return True, "context_ok", details


def _score_extreme_liquidity_capture(
    *,
    timeframe: str,
    secs_to_end: Optional[int],
    metrics: Dict,
    details: Dict,
    cfg: RigidResolvedTickConfigV1,
) -> tuple[int, Dict]:
    leader = _safe_float(metrics.get("leader_buy"), 0.0)
    counter = _safe_float(metrics.get("counter_buy"), 1.0)
    distance_vs_range = details.get("distance_vs_recent_range")
    required_mult = _safe_float(details.get("required_distance_vs_recent_range"), 1.0)
    adverse_5s = _safe_float(details.get("adverse_5s_bps"), 0.0)
    adverse_15s = _safe_float(details.get("adverse_15s_bps"), 0.0)
    depth = _safe_float(metrics.get("leader_depth_top3"), 0.0)
    spread = _safe_float(metrics.get("leader_spread"), 1.0)

    if distance_vs_range is None:
        distance_score = 16
    else:
        distance_score = min(25, int(round(25 * min(1.5, _safe_float(distance_vs_range) / max(required_mult, 0.01)) / 1.5)))

    if timeframe == "5m":
        midpoint = (cfg.min_secs_to_end_5m + cfg.max_secs_to_end_5m) / 2.0
        half_width = max(1.0, (cfg.max_secs_to_end_5m - cfg.min_secs_to_end_5m) / 2.0)
    else:
        midpoint = (cfg.min_secs_to_end_15m + cfg.max_secs_to_end_15m_small) / 2.0
        half_width = max(1.0, (cfg.max_secs_to_end_15m_small - cfg.min_secs_to_end_15m) / 2.0)
    time_score = max(0, int(round(15 * (1.0 - min(1.0, abs(_safe_float(secs_to_end) - midpoint) / half_width)))))

    price_score = 0
    if leader >= 0.99 and counter <= 0.02:
        price_score = 20
    elif leader >= 0.98 and counter <= 0.03:
        price_score = 16
    elif leader >= 0.97 and counter <= 0.03:
        price_score = 10

    reversal_score = max(0, 20 - int(round((adverse_5s / max(cfg.max_adverse_spot_5s_bps, 0.01)) * 10)) - int(round((adverse_15s / max(cfg.max_adverse_spot_15s_bps, 0.01)) * 10)))
    liquidity_score = min(10, int(round(10 * min(1.0, depth / max(cfg.min_depth_top3 * 5.0, 1.0)))))
    spread_score = 10 if spread <= 0.01 else 6 if spread <= 0.02 else 0
    total = max(0, min(100, distance_score + time_score + price_score + reversal_score + liquidity_score + spread_score))
    return total, {
        "distance_score": distance_score,
        "time_score": time_score,
        "price_score": price_score,
        "reversal_score": reversal_score,
        "liquidity_score": liquidity_score,
        "spread_score": spread_score,
    }


def evaluate_rigid_resolved_tick_v1(
    *,
    snap: Dict,
    secs_to_end: Optional[int],
    reference_signal: Optional[Dict],
    state: RigidResolvedTickStateV1,
    slot_key: str,
    timeframe: str = "5m",
    cfg: Optional[RigidResolvedTickConfigV1] = None,
) -> Dict:
    cfg = cfg or RigidResolvedTickConfigV1()
    if slot_key and state.slot_key != slot_key:
        state.reset(slot_key)

    side = _candidate_side(snap, reference_signal)
    target_side = _reference_side(reference_signal)
    book_side = _book_dominant_side(snap)
    result = {
        "setup": "passive_extreme_liquidity_capture",
        "allow": False,
        "action": "WAIT",
        "phase": state.phase,
        "side": side,
        "target_side": target_side,
        "book_side": book_side,
        "reason": "no_side",
        "timeframe": timeframe,
        "secs_to_end": secs_to_end,
        "limit_price": None,
        "leader_price": None,
        "counter_price": None,
        "tick_size": None,
        "score": 0,
        "status": "WAIT",
        "size_label": None,
        "state": state.as_dict(),
    }
    if side not in ("UP", "DOWN"):
        state.last_reason = "no_side"
        return result

    metrics = _side_metrics(snap, side)
    result.update(
        {
            "leader_price": metrics["leader_buy"],
            "leader_bid": metrics["leader_sell"],
            "leader_ask": metrics["leader_ask"],
            "counter_price": metrics["counter_buy"],
            "counter_bid": metrics["counter_bid"],
            "counter_ask": metrics["counter_ask"],
            "screen_odd": _round_price(1.0 - metrics["leader_buy"]) if metrics["leader_buy"] >= 0 else None,
            "tick_size": metrics["tick_size"],
            "leader_depth_top3": metrics["leader_depth_top3"],
            "leader_spread": metrics["leader_spread"],
        }
    )
    ok, reason, details = _context_ok(
        side=side,
        timeframe=timeframe,
        secs_to_end=secs_to_end,
        reference_signal=reference_signal,
        metrics=metrics,
        book_side=book_side,
        cfg=cfg,
    )
    result.update(details)
    result["size_label"] = details.get("size_label")

    leader = metrics["leader_buy"]
    tick = max(metrics["tick_size"] or cfg.tick_size_default, cfg.tick_size_default)
    previous_leader = state.last_leader_price
    passive_limit = _one_cent_below(leader)
    passive_price_ok = passive_limit > 0 and passive_limit < leader

    if not ok:
        cancel_open_order = state.phase in ("order", "chase")
        state.side = side
        state.phase = "watch"
        state.armed_after_price = None
        state.order_price = None
        state.last_leader_price = leader if leader > 0 else previous_leader
        state.last_reason = reason
        result.update(
            {
                "action": "CANCEL" if cancel_open_order else "WAIT",
                "status": "CANCEL" if cancel_open_order else "HIGH_RISK",
                "reason": reason,
                "phase": state.phase,
                "state": state.as_dict(),
            }
        )
        return result

    if state.side not in (None, side):
        state.reset(slot_key)
    state.side = side

    score, score_components = _score_extreme_liquidity_capture(
        timeframe=timeframe,
        secs_to_end=secs_to_end,
        metrics=metrics,
        details=details,
        cfg=cfg,
    )
    result["score"] = score
    result["score_components"] = score_components
    if score < cfg.min_entry_score:
        cancel_open_order = state.phase in ("order", "chase")
        if cancel_open_order:
            state.phase = "watch"
            state.order_price = None
            action = "CANCEL"
            status = "CANCEL"
            reason = "score_dropped_below_entry_threshold"
        elif score >= cfg.observe_score:
            action = "OBSERVE"
            status = "AGUARDAR"
            reason = "score_observe_only"
        else:
            action = "WAIT"
            status = "RISK_HIGH"
            reason = "score_too_low"
        state.last_leader_price = leader
        state.last_reason = reason
        result.update(
            {
                "allow": False,
                "action": action,
                "status": status,
                "phase": state.phase,
                "reason": reason,
                "limit_price": state.order_price,
                "state": state.as_dict(),
            }
        )
        return result

    tick_up = previous_leader is not None and leader >= previous_leader + (tick * 0.9)
    if state.phase == "watch":
        if leader >= cfg.min_leader_price and tick_up and passive_price_ok:
            state.phase = "order"
            state.armed_after_price = leader
            state.order_price = passive_limit
            action = "PLACE_LIMIT"
            status = "ENTRAR"
            reason = "passive_tick_up_confirmed_place_one_cent_below"
        else:
            state.phase = "armed"
            state.armed_after_price = cfg.min_leader_price
            state.order_price = None
            action = "ARM"
            status = "AGUARDAR"
            reason = "wait_for_resolved_tick_up_before_passive_order"
    elif state.phase == "armed":
        if leader >= cfg.min_leader_price and tick_up and passive_price_ok:
            state.phase = "order"
            state.armed_after_price = leader
            state.order_price = passive_limit
            action = "PLACE_LIMIT"
            status = "ENTRAR"
            reason = "passive_tick_up_confirmed_place_one_cent_below"
        else:
            action = "ARM"
            status = "AGUARDAR"
            reason = "conditions_ok_waiting_passive_tick_up"
    else:
        if state.order_price is not None and leader >= cfg.chase_leader_price and state.order_price < cfg.preferred_leader_price:
            state.phase = "chase"
            state.order_price = cfg.preferred_leader_price
            action = "CANCEL_REPLACE"
            status = "ENTRAR"
            reason = "leader_reached_100_chase_with_99"
        elif leader < cfg.cancel_if_leader_below:
            state.phase = "watch"
            state.order_price = None
            action = "CANCEL"
            status = "CANCELAR"
            reason = "leader_lost_resolved_price"
        else:
            action = "KEEP_LIMIT"
            status = "AGUARDAR"
            reason = "limit_working_wait_fill_or_resolution"

    state.last_leader_price = leader
    state.last_reason = reason
    result.update(
        {
            "allow": action in ("PLACE_LIMIT", "KEEP_LIMIT", "CANCEL_REPLACE"),
            "action": action,
            "status": status,
            "phase": state.phase,
            "reason": reason,
            "limit_price": state.order_price,
            "stop_price": cfg.stop_price,
            "target_price": cfg.target_price,
            "state": state.as_dict(),
        }
    )
    return result
