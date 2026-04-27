from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, Optional


@dataclass
class CurrentEarlyOverresolvedConfigV1:
    qty: int = 50
    leader_price_min: float = 0.88
    leader_price_extreme: float = 0.95
    counter_price_max: float = 0.10
    min_secs_to_end_default: int = 120
    hard_floor_secs_to_end: int = 45
    min_elapsed_default: int = 20
    min_elapsed_by_tf_15m: int = 60
    min_distance_from_open_bps_5m: float = 1.8
    min_distance_from_open_bps_15m: float = 2.5
    min_market_range_30s: float = 0.06
    min_market_range_60s_15m: float = 0.06
    min_spot_reversal_5s_bps: float = 0.30
    min_market_pullback_5s: float = 0.03
    max_spread_counter: float = 0.02
    target_ticks: int = 2
    stop_ticks: int = 1
    max_hold_secs_5m: int = 25
    max_hold_secs_15m: int = 45
    late_exception_distance_vs_range: float = 1.15

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class _Sample:
    ts: float
    reference_price: float
    up_mid: float


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _bps_change(now_price: Optional[float], base_price: Optional[float]) -> Optional[float]:
    if now_price is None or base_price is None or base_price <= 0:
        return None
    return round(((float(now_price) / float(base_price)) - 1.0) * 10000.0, 4)


def _find_before(samples: Deque[_Sample], now_ts: float, lookback_secs: int) -> Optional[_Sample]:
    cutoff = now_ts - lookback_secs
    candidate = None
    for sample in samples:
        if sample.ts <= cutoff:
            candidate = sample
        else:
            break
    return candidate


def _range_up_mid(samples: Deque[_Sample], now_ts: float, lookback_secs: int) -> Optional[float]:
    cutoff = now_ts - lookback_secs
    values = [float(sample.up_mid) for sample in samples if sample.ts >= cutoff]
    if not values:
        return None
    return round(max(values) - min(values), 6)


def _mid_from_side(side_book: dict | None) -> Optional[float]:
    if not side_book:
        return None
    bid = _safe_float(side_book.get("executable_sell") or side_book.get("best_bid"), -1.0)
    ask = _safe_float(side_book.get("executable_buy") or side_book.get("best_ask"), -1.0)
    if bid <= 0 or ask <= 0:
        return None
    lo = min(bid, ask)
    hi = max(bid, ask)
    return round((lo + hi) / 2.0, 6)


class CurrentEarlyOverresolvedResearchV1:
    def __init__(self, cfg: Optional[CurrentEarlyOverresolvedConfigV1] = None, mode: str = "strict"):
        self.cfg = cfg or CurrentEarlyOverresolvedConfigV1()
        self.mode = str(mode).lower()
        self.samples_by_key: Dict[str, Deque[_Sample]] = {}

    @classmethod
    def from_mode(cls, *, qty: int = 50, mode: str = "strict") -> "CurrentEarlyOverresolvedResearchV1":
        cfg = CurrentEarlyOverresolvedConfigV1(qty=max(1, int(qty)))
        if str(mode).lower() == "flex":
            cfg.leader_price_min = 0.64
            cfg.leader_price_extreme = 0.82
            cfg.counter_price_max = 0.36
            cfg.min_secs_to_end_default = 80
            cfg.hard_floor_secs_to_end = 35
            cfg.min_elapsed_default = 10
            cfg.min_elapsed_by_tf_15m = 30
            cfg.min_distance_from_open_bps_5m = 0.45
            cfg.min_distance_from_open_bps_15m = 0.9
            cfg.min_market_range_30s = 0.0
            cfg.min_market_range_60s_15m = 0.01
            cfg.min_spot_reversal_5s_bps = 0.03
            cfg.min_market_pullback_5s = 0.0
            cfg.max_spread_counter = 0.03
            cfg.target_ticks = 1
            cfg.stop_ticks = 1
            cfg.max_hold_secs_5m = 18
            cfg.max_hold_secs_15m = 30
            cfg.late_exception_distance_vs_range = 1.6
        return cls(cfg=cfg, mode=mode)

    def evaluate(
        self,
        *,
        timeframe: str,
        slug: str,
        snap: dict,
        secs_to_end: Optional[int],
        event_start_time: Optional[str],
        opening_reference_price: Optional[float],
        reference_payload: dict,
        now_ts: float,
    ) -> dict:
        cfg = self.cfg
        up = snap.get("up") or {}
        down = snap.get("down") or {}
        up_mid = _mid_from_side(up)
        down_mid = _mid_from_side(down)
        ref_price = reference_payload.get("reference_price")
        result = {
            "setup": "early_overresolved_reversal",
            "allow": False,
            "timeframe": timeframe,
            "slug": slug,
            "side": None,
            "reason": "no_signal",
            "secs_to_end": secs_to_end,
            "opening_reference_price": opening_reference_price,
            "reference_price": ref_price,
            "source_divergence_bps": reference_payload.get("source_divergence_bps"),
            "up_mid": up_mid,
            "down_mid": down_mid,
            "up_bid": _safe_float(up.get("executable_sell") or up.get("best_bid"), 0.0),
            "up_ask": _safe_float(up.get("executable_buy") or up.get("best_ask"), 0.0),
            "down_bid": _safe_float(down.get("executable_sell") or down.get("best_bid"), 0.0),
            "down_ask": _safe_float(down.get("executable_buy") or down.get("best_ask"), 0.0),
        }
        if ref_price is None or opening_reference_price is None or up_mid is None or down_mid is None:
            result["reason"] = "missing_reference_or_mid"
            return result
        if secs_to_end is None or secs_to_end <= 0:
            result["reason"] = "missing_secs_to_end"
            return result

        distance_from_open_bps = _bps_change(ref_price, opening_reference_price)
        result["distance_from_open_bps"] = distance_from_open_bps
        key = f"{timeframe}:{slug}"
        samples = self.samples_by_key.setdefault(key, deque())
        samples.append(_Sample(ts=now_ts, reference_price=float(ref_price), up_mid=float(up_mid)))
        cutoff = now_ts - 120
        while samples and samples[0].ts < cutoff:
            samples.popleft()

        s5 = _find_before(samples, now_ts, 5)
        s15 = _find_before(samples, now_ts, 15)
        market_delta_5s = round(float(up_mid) - float(s5.up_mid), 6) if s5 else None
        market_delta_15s = round(float(up_mid) - float(s15.up_mid), 6) if s15 else None
        spot_delta_5s_bps = _bps_change(ref_price, s5.reference_price if s5 else None)
        spot_delta_15s_bps = _bps_change(ref_price, s15.reference_price if s15 else None)
        lookback_window = 60 if timeframe == "15m" else 30
        market_range_window = _range_up_mid(samples, now_ts, lookback_window)
        result["spot_delta_5s_bps"] = spot_delta_5s_bps
        result["spot_delta_15s_bps"] = spot_delta_15s_bps
        result["market_delta_5s"] = market_delta_5s
        result["market_delta_15s"] = market_delta_15s
        result["market_range_window"] = market_range_window

        elapsed_from_open = None
        if event_start_time:
            try:
                event_start_dt = datetime.fromisoformat(str(event_start_time).replace("Z", "+00:00"))
                elapsed_from_open = max(0, int(round((datetime.now(timezone.utc) - event_start_dt).total_seconds())))
            except Exception:
                elapsed_from_open = None
        result["elapsed_from_open_secs"] = elapsed_from_open

        min_elapsed = cfg.min_elapsed_by_tf_15m if timeframe == "15m" else cfg.min_elapsed_default
        if elapsed_from_open is not None and elapsed_from_open < min_elapsed:
            result["reason"] = "too_early_after_open"
            return result

        leader_side = "UP" if float(up_mid) >= float(down_mid) else "DOWN"
        leader_ask = result["up_ask"] if leader_side == "UP" else result["down_ask"]
        counter_bid = result["down_bid"] if leader_side == "UP" else result["up_bid"]
        counter_ask = result["down_ask"] if leader_side == "UP" else result["up_ask"]
        counter_spread = round(max(0.0, counter_ask - counter_bid), 6) if counter_bid > 0 and counter_ask > 0 else 0.0
        result["leader_side"] = leader_side
        result["leader_ask"] = leader_ask
        result["counter_ask"] = counter_ask
        result["counter_spread"] = counter_spread

        min_distance = cfg.min_distance_from_open_bps_15m if timeframe == "15m" else cfg.min_distance_from_open_bps_5m
        min_range = cfg.min_market_range_60s_15m if timeframe == "15m" else cfg.min_market_range_30s
        if distance_from_open_bps is None or abs(distance_from_open_bps) < min_distance:
            result["reason"] = f"distance_from_open_too_small={distance_from_open_bps}"
            return result
        if market_range_window is None or market_range_window < min_range:
            result["reason"] = f"market_range_window_too_small={market_range_window}"
            return result
        if leader_ask < cfg.leader_price_min or counter_ask > cfg.counter_price_max:
            result["reason"] = f"not_overresolved_enough leader={leader_ask} counter={counter_ask}"
            return result
        if counter_spread > cfg.max_spread_counter:
            result["reason"] = f"counter_spread_too_wide={counter_spread}"
            return result

        late_exception_allowed = False
        if secs_to_end < cfg.min_secs_to_end_default:
            spot_range_bps = abs(_safe_float(spot_delta_15s_bps, 0.0)) + max(0.5, float(market_range_window) * 10000.0 * 0.35)
            if secs_to_end >= cfg.hard_floor_secs_to_end and abs(distance_from_open_bps) <= spot_range_bps * cfg.late_exception_distance_vs_range:
                late_exception_allowed = True
            else:
                result["reason"] = f"too_late_without_exception secs={secs_to_end}"
                return result
        result["late_exception_allowed"] = late_exception_allowed

        if leader_side == "UP":
            reversal_seen = (
                _safe_float(spot_delta_5s_bps, 0.0) <= -cfg.min_spot_reversal_5s_bps
                or _safe_float(market_delta_5s, 0.0) <= -cfg.min_market_pullback_5s
                or _safe_float(market_delta_15s, 0.0) < 0.0
            )
            if self.mode != "flex" and not reversal_seen:
                result["reason"] = "up_leader_not_reversing_yet"
                return result
            if self.mode == "flex" and not reversal_seen and leader_ask < cfg.leader_price_extreme:
                result["reason"] = "up_leader_not_reversing_yet"
                return result
            result.update(
                {
                    "allow": True,
                    "side": "DOWN",
                    "reason": "up_leader_overresolved_reversal_flex" if self.mode == "flex" else "up_leader_overresolved_reversal",
                    "entry_price": counter_ask,
                    "target_price": round(min(0.99, counter_ask + cfg.target_ticks * 0.01), 6),
                    "stop_price": round(max(0.01, counter_ask - cfg.stop_ticks * 0.01), 6),
                }
            )
            return result

        reversal_seen = (
            _safe_float(spot_delta_5s_bps, 0.0) >= cfg.min_spot_reversal_5s_bps
            or _safe_float(market_delta_5s, 0.0) >= cfg.min_market_pullback_5s
            or _safe_float(market_delta_15s, 0.0) > 0.0
        )
        if self.mode != "flex" and not reversal_seen:
            result["reason"] = "down_leader_not_reversing_yet"
            return result
        if self.mode == "flex" and not reversal_seen and leader_ask < cfg.leader_price_extreme:
            result["reason"] = "down_leader_not_reversing_yet"
            return result
        result.update(
            {
                "allow": True,
                "side": "UP",
                "reason": "down_leader_overresolved_reversal_flex" if self.mode == "flex" else "down_leader_overresolved_reversal",
                "entry_price": counter_ask,
                "target_price": round(min(0.99, counter_ask + cfg.target_ticks * 0.01), 6),
                "stop_price": round(max(0.01, counter_ask - cfg.stop_ticks * 0.01), 6),
            }
        )
        return result
