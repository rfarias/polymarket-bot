from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from typing import Deque, Dict, List, Optional


@dataclass
class Next1ScalpConfigV1:
    min_secs_to_end: int = 330
    max_secs_to_end: int = 780
    max_spread: float = 0.03
    min_depth_top3: float = 8.0
    max_source_divergence_bps: float = 8.0
    current_trend_bps: float = 4.0
    spot_momentum_5s_bps: float = 1.0
    spot_momentum_15s_bps: float = 2.0
    spot_support_5s_bps: float = 0.5
    next1_discount_price: float = 0.49
    next1_chase_price_cap: float = 0.49
    aggressive_entry_soft_cap: float = 0.49
    aggressive_entry_hard_cap: float = 0.49
    current_up_delta_min: float = 0.02
    lag_delta_min: float = 0.01
    strong_current_up_delta_min: float = 0.08
    strong_lag_delta_min: float = 0.06
    high_price_current_delta_min: float = 0.05
    high_price_lag_delta_min: float = 0.03
    high_price_spot_momentum_5s_bps: float = 1.5
    extreme_current_price_cap: float = 0.12
    extreme_current_price_floor: float = 0.88
    max_spot_opposition_5s_bps: float = 0.5
    extreme_next1_chase_price_cap: float = 0.49
    reversal_extension_bps: float = 7.0
    reversal_rebound_5s_bps: float = 2.0
    target_ticks: int = 2
    stop_ticks: int = 2
    max_hold_secs: int = 60
    handoff_cancel_secs: int = 30
    flatten_deadline_secs: int = 1
    current_transition_ignore_secs: int = 30

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class _Sample:
    ts: float
    reference_price: float
    current_up_mid: float
    next1_up_mid: float


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _sum_depth(levels: Optional[List[Dict]], top_n: int = 3) -> float:
    if not levels:
        return 0.0
    total = 0.0
    for lvl in levels[:top_n]:
        total += _safe_float((lvl or {}).get("size"), 0.0)
    return round(total, 6)


def _mid_from_snap(side: Dict) -> Optional[float]:
    bid = side.get("executable_sell")
    ask = side.get("executable_buy")
    if bid is None or ask is None:
        bid = side.get("best_bid")
        ask = side.get("best_ask")
    bid = _safe_float(bid, -1.0)
    ask = _safe_float(ask, -1.0)
    if bid <= 0 or ask <= 0:
        return None
    lo = min(bid, ask)
    hi = max(bid, ask)
    return round((lo + hi) / 2.0, 6)


def _tick_from_side(side: Dict) -> float:
    return max(0.001, _safe_float(side.get("tick_size"), 0.01))


def _improved_entry_price(ask_price: float, side: Dict) -> float:
    tick = _tick_from_side(side)
    return round(max(0.01, ask_price - (2.0 * tick)), 6)


def _bps_change(now_value: Optional[float], old_value: Optional[float]) -> Optional[float]:
    if now_value is None or old_value is None or old_value <= 0:
        return None
    return round(((float(now_value) / float(old_value)) - 1.0) * 10000.0, 4)


def _aggressive_entry_allowed(
    *,
    price: float,
    current_delta: float,
    lag_delta: float,
    spot_momentum_5s_bps: float,
    cfg: Next1ScalpConfigV1,
) -> bool:
    if price <= 0:
        return False
    if price > cfg.aggressive_entry_hard_cap:
        return False
    if price <= cfg.aggressive_entry_soft_cap:
        return True
    return (
        current_delta >= cfg.high_price_current_delta_min
        and lag_delta >= cfg.high_price_lag_delta_min
        and spot_momentum_5s_bps >= cfg.high_price_spot_momentum_5s_bps
    )


def _find_before(samples: Deque[_Sample], now_ts: float, lookback_secs: int) -> Optional[_Sample]:
    cutoff = now_ts - lookback_secs
    candidate = None
    for sample in samples:
        if sample.ts <= cutoff:
            candidate = sample
        else:
            break
    return candidate


class Next1ScalpResearchV1:
    def __init__(self, cfg: Optional[Next1ScalpConfigV1] = None, history_secs: int = 120):
        self.cfg = cfg or Next1ScalpConfigV1()
        self.history_secs = max(30, int(history_secs))
        self.samples: Deque[_Sample] = deque()

    def _trim(self, now_ts: float) -> None:
        cutoff = now_ts - self.history_secs
        while self.samples and self.samples[0].ts < cutoff:
            self.samples.popleft()

    def evaluate(
        self,
        *,
        current_snap: Dict,
        next1_snap: Dict,
        current_secs: Optional[int],
        next1_secs: Optional[int],
        reference_price: Optional[float],
        source_divergence_bps: Optional[float],
        now_ts: float,
    ) -> Dict:
        cfg = self.cfg
        cur_up = current_snap.get("up") or {}
        cur_down = current_snap.get("down") or {}
        n1_up = next1_snap.get("up") or {}
        n1_down = next1_snap.get("down") or {}

        current_up_mid = _mid_from_snap(cur_up)
        current_down_mid = _mid_from_snap(cur_down)
        next1_up_mid = _mid_from_snap(n1_up)
        next1_down_mid = _mid_from_snap(n1_down)

        result = {
            "setup": "no_edge",
            "allow": False,
            "side": None,
            "reason": "no_signal",
            "entry_price": None,
            "exit_price": None,
            "current_secs": current_secs,
            "next1_secs": next1_secs,
            "reference_price": reference_price,
            "source_divergence_bps": source_divergence_bps,
            "current_up_mid": current_up_mid,
            "current_down_mid": current_down_mid,
            "next1_up_mid": next1_up_mid,
            "next1_down_mid": next1_down_mid,
            "spot_delta_5s_bps": None,
            "spot_delta_15s_bps": None,
            "current_up_delta_15s": None,
            "next1_up_delta_15s": None,
            "lag_up_15s": None,
        }

        if reference_price is None or current_up_mid is None or next1_up_mid is None or current_down_mid is None or next1_down_mid is None:
            result["reason"] = "missing_reference_or_market_mid"
            return result
        if next1_secs is None or next1_secs < cfg.min_secs_to_end or next1_secs > cfg.max_secs_to_end:
            result["reason"] = "next1_outside_scalp_window"
            return result
        if current_secs is None or current_secs <= 0:
            result["reason"] = "current_unavailable"
            return result
        if source_divergence_bps is not None and source_divergence_bps > cfg.max_source_divergence_bps:
            result["reason"] = f"source_divergence_too_high={source_divergence_bps}"
            return result

        self.samples.append(
            _Sample(
                ts=now_ts,
                reference_price=float(reference_price),
                current_up_mid=float(current_up_mid),
                next1_up_mid=float(next1_up_mid),
            )
        )
        self._trim(now_ts)

        s5 = _find_before(self.samples, now_ts, 5)
        s15 = _find_before(self.samples, now_ts, 15)
        spot_delta_5s_bps = _bps_change(reference_price, s5.reference_price if s5 else None)
        spot_delta_15s_bps = _bps_change(reference_price, s15.reference_price if s15 else None)
        current_up_delta_15s = round(float(current_up_mid) - float(s15.current_up_mid), 6) if s15 else None
        next1_up_delta_15s = round(float(next1_up_mid) - float(s15.next1_up_mid), 6) if s15 else None
        lag_up_15s = round(float(current_up_delta_15s or 0.0) - float(next1_up_delta_15s or 0.0), 6) if s15 else None

        result.update(
            {
                "spot_delta_5s_bps": spot_delta_5s_bps,
                "spot_delta_15s_bps": spot_delta_15s_bps,
                "current_up_delta_15s": current_up_delta_15s,
                "next1_up_delta_15s": next1_up_delta_15s,
                "lag_up_15s": lag_up_15s,
            }
        )

        n1_up_buy = _safe_float(n1_up.get("executable_buy") or n1_up.get("best_ask"), -1.0)
        n1_up_sell = _safe_float(n1_up.get("executable_sell") or n1_up.get("best_bid"), -1.0)
        n1_down_buy = _safe_float(n1_down.get("executable_buy") or n1_down.get("best_ask"), -1.0)
        n1_down_sell = _safe_float(n1_down.get("executable_sell") or n1_down.get("best_bid"), -1.0)
        n1_up_passive_buy = _improved_entry_price(n1_up_buy, n1_up) if n1_up_buy > 0 else None
        n1_down_passive_buy = _improved_entry_price(n1_down_buy, n1_down) if n1_down_buy > 0 else None
        spread_up = round(max(0.0, max(n1_up_buy, n1_up_sell) - min(n1_up_buy, n1_up_sell)), 6)
        spread_down = round(max(0.0, max(n1_down_buy, n1_down_sell) - min(n1_down_buy, n1_down_sell)), 6)
        depth_top3 = round(
            _sum_depth(n1_up.get("top_bids"), 3)
            + _sum_depth(n1_up.get("top_asks"), 3)
            + _sum_depth(n1_down.get("top_bids"), 3)
            + _sum_depth(n1_down.get("top_asks"), 3),
            6,
        )
        result["spread_up"] = spread_up
        result["spread_down"] = spread_down
        result["next1_depth_top3"] = depth_top3
        result["up_entry_aggressive_price"] = n1_up_buy if n1_up_buy > 0 else None
        result["down_entry_aggressive_price"] = n1_down_buy if n1_down_buy > 0 else None
        result["up_entry_passive_price"] = n1_up_passive_buy
        result["down_entry_passive_price"] = n1_down_passive_buy
        if max(spread_up, spread_down) > cfg.max_spread:
            result["reason"] = f"spread_too_wide={max(spread_up, spread_down)}"
            return result
        if depth_top3 < cfg.min_depth_top3:
            result["reason"] = f"depth_too_low={depth_top3}"
            return result
        if s15 is None:
            result["reason"] = "warming_up_need_15s"
            return result

        current_down_delta_15s = round(-float(current_up_delta_15s or 0.0), 6) if current_up_delta_15s is not None else None
        next1_down_delta_15s = round(-float(next1_up_delta_15s or 0.0), 6) if next1_up_delta_15s is not None else None
        lag_down_15s = round(float(current_down_delta_15s or 0.0) - float(next1_down_delta_15s or 0.0), 6) if s15 else None
        result["current_down_delta_15s"] = current_down_delta_15s
        result["next1_down_delta_15s"] = next1_down_delta_15s
        result["lag_down_15s"] = lag_down_15s

        # Continuation on the up side: current and spot are leading, next1 still acceptable.
        if (
            (spot_delta_5s_bps or 0.0) >= cfg.spot_momentum_5s_bps
            and (spot_delta_15s_bps or 0.0) >= cfg.spot_momentum_15s_bps
            and (current_up_delta_15s or 0.0) >= cfg.current_up_delta_min
            and (lag_up_15s or 0.0) >= cfg.lag_delta_min
            and n1_up_buy > 0
            and n1_up_buy <= cfg.next1_chase_price_cap
            and _aggressive_entry_allowed(
                price=n1_up_buy,
                current_delta=(current_up_delta_15s or 0.0),
                lag_delta=(lag_up_15s or 0.0),
                spot_momentum_5s_bps=(spot_delta_5s_bps or 0.0),
                cfg=cfg,
            )
        ):
            result.update(
                {
                    "setup": "continuation",
                    "allow": True,
                    "side": "UP",
                    "reason": "current_and_spot_lead_next1_up",
                    "entry_price": n1_up_passive_buy,
                    "aggressive_entry_price": n1_up_buy,
                    "exit_price": n1_up_sell,
                }
            )
            return result

        # Softer continuation when current is clearly dragging next1 and spot at least confirms direction.
        if (
            (spot_delta_5s_bps or 0.0) >= cfg.spot_support_5s_bps
            and (current_up_delta_15s or 0.0) >= cfg.strong_current_up_delta_min
            and (lag_up_15s or 0.0) >= cfg.strong_lag_delta_min
            and n1_up_buy > 0
            and n1_up_buy <= cfg.next1_chase_price_cap
            and _aggressive_entry_allowed(
                price=n1_up_buy,
                current_delta=(current_up_delta_15s or 0.0),
                lag_delta=(lag_up_15s or 0.0),
                spot_momentum_5s_bps=(spot_delta_5s_bps or 0.0),
                cfg=cfg,
            )
        ):
            result.update(
                {
                    "setup": "continuation_soft",
                    "allow": True,
                    "side": "UP",
                    "reason": "current_leads_next1_up_with_spot_support",
                    "entry_price": n1_up_passive_buy,
                    "aggressive_entry_price": n1_up_buy,
                    "exit_price": n1_up_sell,
                }
            )
            return result

        if (
            current_up_mid >= cfg.extreme_current_price_floor
            and (lag_up_15s or 0.0) >= cfg.strong_lag_delta_min
            and (spot_delta_5s_bps or 0.0) >= -cfg.max_spot_opposition_5s_bps
            and n1_up_buy > 0
            and n1_up_buy <= cfg.extreme_next1_chase_price_cap
            and _aggressive_entry_allowed(
                price=n1_up_buy,
                current_delta=(current_up_delta_15s or 0.0),
                lag_delta=(lag_up_15s or 0.0),
                spot_momentum_5s_bps=abs(spot_delta_5s_bps or 0.0),
                cfg=cfg,
            )
        ):
            result.update(
                {
                    "setup": "continuation_extreme",
                    "allow": True,
                    "side": "UP",
                    "reason": "extreme_current_up_dragging_next1",
                    "entry_price": n1_up_passive_buy,
                    "aggressive_entry_price": n1_up_buy,
                    "exit_price": n1_up_sell,
                }
            )
            return result

        # Discount reversal into up if next1 is cheap while current/spot stop selling off.
        if (
            n1_up_buy > 0
            and n1_up_buy <= cfg.next1_discount_price
            and (spot_delta_15s_bps or 0.0) <= -cfg.reversal_extension_bps
            and (spot_delta_5s_bps or 0.0) >= cfg.reversal_rebound_5s_bps
        ):
            result.update(
                {
                    "setup": "reversal",
                    "allow": True,
                    "side": "UP",
                    "reason": "discounted_next1_up_after_spot_flush",
                    "entry_price": n1_up_passive_buy,
                    "aggressive_entry_price": n1_up_buy,
                    "exit_price": n1_up_sell,
                }
            )
            return result

        if (
            (spot_delta_5s_bps or 0.0) <= -cfg.spot_momentum_5s_bps
            and (spot_delta_15s_bps or 0.0) <= -cfg.spot_momentum_15s_bps
            and (current_down_delta_15s or 0.0) >= cfg.current_up_delta_min
            and (lag_down_15s or 0.0) >= cfg.lag_delta_min
            and n1_down_buy > 0
            and n1_down_buy <= cfg.next1_chase_price_cap
            and _aggressive_entry_allowed(
                price=n1_down_buy,
                current_delta=(current_down_delta_15s or 0.0),
                lag_delta=(lag_down_15s or 0.0),
                spot_momentum_5s_bps=abs(spot_delta_5s_bps or 0.0),
                cfg=cfg,
            )
        ):
            result.update(
                {
                    "setup": "continuation",
                    "allow": True,
                    "side": "DOWN",
                    "reason": "current_and_spot_lead_next1_down",
                    "entry_price": n1_down_passive_buy,
                    "aggressive_entry_price": n1_down_buy,
                    "exit_price": n1_down_sell,
                }
            )
            return result

        if (
            (spot_delta_5s_bps or 0.0) <= -cfg.spot_support_5s_bps
            and (current_down_delta_15s or 0.0) >= cfg.strong_current_up_delta_min
            and (lag_down_15s or 0.0) >= cfg.strong_lag_delta_min
            and n1_down_buy > 0
            and n1_down_buy <= cfg.next1_chase_price_cap
            and _aggressive_entry_allowed(
                price=n1_down_buy,
                current_delta=(current_down_delta_15s or 0.0),
                lag_delta=(lag_down_15s or 0.0),
                spot_momentum_5s_bps=abs(spot_delta_5s_bps or 0.0),
                cfg=cfg,
            )
        ):
            result.update(
                {
                    "setup": "continuation_soft",
                    "allow": True,
                    "side": "DOWN",
                    "reason": "current_leads_next1_down_with_spot_support",
                    "entry_price": n1_down_passive_buy,
                    "aggressive_entry_price": n1_down_buy,
                    "exit_price": n1_down_sell,
                }
            )
            return result

        if (
            current_up_mid <= cfg.extreme_current_price_cap
            and (lag_down_15s or 0.0) >= cfg.strong_lag_delta_min
            and (spot_delta_5s_bps or 0.0) <= cfg.max_spot_opposition_5s_bps
            and n1_down_buy > 0
            and n1_down_buy <= cfg.extreme_next1_chase_price_cap
            and _aggressive_entry_allowed(
                price=n1_down_buy,
                current_delta=(current_down_delta_15s or 0.0),
                lag_delta=(lag_down_15s or 0.0),
                spot_momentum_5s_bps=abs(spot_delta_5s_bps or 0.0),
                cfg=cfg,
            )
        ):
            result.update(
                {
                    "setup": "continuation_extreme",
                    "allow": True,
                    "side": "DOWN",
                    "reason": "extreme_current_down_dragging_next1",
                    "entry_price": n1_down_passive_buy,
                    "aggressive_entry_price": n1_down_buy,
                    "exit_price": n1_down_sell,
                }
            )
            return result

        if (
            n1_down_buy > 0
            and n1_down_buy <= cfg.next1_discount_price
            and (spot_delta_15s_bps or 0.0) >= cfg.reversal_extension_bps
            and (spot_delta_5s_bps or 0.0) <= -cfg.reversal_rebound_5s_bps
        ):
            result.update(
                {
                    "setup": "reversal",
                    "allow": True,
                    "side": "DOWN",
                    "reason": "discounted_next1_down_after_spot_spike",
                    "entry_price": n1_down_passive_buy,
                    "aggressive_entry_price": n1_down_buy,
                    "exit_price": n1_down_sell,
                }
            )
            return result

        result["reason"] = "no_next1_scalp_edge"
        return result
