from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Deque, Dict, List, Optional

import requests


BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
TIMEOUT = 10


@dataclass
class CurrentScalpConfigV1:
    min_elapsed_from_open_secs: int = 45
    min_secs_to_end: int = 25
    max_spread: float = 0.03
    min_depth_top3: float = 10.0
    max_source_divergence_bps: float = 6.0
    trend_distance_bps: float = 5.0
    continuation_momentum_5s_bps: float = 1.0
    continuation_momentum_15s_bps: float = 2.0
    reversal_extension_bps: float = 7.0
    reversal_bounce_5s_bps: float = 2.5
    reversal_reclaim_15s_bps: float = 0.5
    continuation_price_cap: float = 0.72
    reversal_price_cap: float = 0.40
    max_hold_secs: int = 20
    stop_ticks: int = 2
    target_ticks: int = 1

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class _SpotSample:
    ts: float
    reference_price: float
    up_mid: float
    up_buy: float = 0.0
    down_buy: float = 0.0


@dataclass
class _MicroBookSample:
    ts: float
    reference_price: float
    up_mid: float
    up_bid_depth: float
    up_ask_depth: float
    down_bid_depth: float
    down_ask_depth: float


def _safe_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _sum_depth(levels: Optional[List[Dict]], top_n: int = 3) -> float:
    if not levels:
        return 0.0
    total = 0.0
    for lvl in levels[:top_n]:
        size = _safe_float((lvl or {}).get("size"))
        if size is not None and size > 0:
            total += size
    return round(total, 6)


def _depth_imbalance(bid_depth: float, ask_depth: float) -> float:
    total = float(bid_depth) + float(ask_depth)
    if total <= 0:
        return 0.0
    return round((float(bid_depth) - float(ask_depth)) / total, 6)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _bps_change(now_price: Optional[float], base_price: Optional[float]) -> Optional[float]:
    if now_price is None or base_price is None or base_price <= 0:
        return None
    return round(((float(now_price) / float(base_price)) - 1.0) * 10000.0, 4)


def _find_sample_before(samples: Deque[_SpotSample], now_ts: float, lookback_secs: int) -> Optional[_SpotSample]:
    cutoff = now_ts - lookback_secs
    candidate = None
    for sample in samples:
        if sample.ts <= cutoff:
            candidate = sample
        else:
            break
    return candidate


def _range_up_mid(samples: Deque[_SpotSample], now_ts: float, lookback_secs: int) -> Optional[float]:
    cutoff = now_ts - lookback_secs
    values = [float(sample.up_mid) for sample in samples if sample.ts >= cutoff]
    if not values:
        return None
    return round(max(values) - min(values), 6)


def _range_reference_price(samples: Deque[_SpotSample], now_ts: float, lookback_secs: int) -> Optional[float]:
    cutoff = now_ts - lookback_secs
    values = [float(sample.reference_price) for sample in samples if sample.ts >= cutoff]
    if not values:
        return None
    return round(max(values) - min(values), 6)


def _max_buy_price(samples: Deque[_SpotSample], now_ts: float, lookback_secs: int, side: str) -> Optional[float]:
    cutoff = now_ts - lookback_secs
    if side == "UP":
        values = [float(sample.up_buy) for sample in samples if sample.ts >= cutoff and sample.up_buy > 0]
    else:
        values = [float(sample.down_buy) for sample in samples if sample.ts >= cutoff and sample.down_buy > 0]
    if not values:
        return None
    return round(max(values), 6)


def _window_up_mid_values(samples: Deque[_SpotSample], now_ts: float, lookback_secs: int) -> List[float]:
    cutoff = now_ts - lookback_secs
    return [float(sample.up_mid) for sample in samples if sample.ts >= cutoff]


def _range_position(current_value: Optional[float], values: List[float]) -> Optional[float]:
    if current_value is None or not values:
        return None
    low = min(values)
    high = max(values)
    width = high - low
    if width <= 0:
        return 0.5
    return round((float(current_value) - low) / width, 4)


def _sign_changes(values: List[float], min_step: float = 0.001) -> int:
    previous_sign = 0
    flips = 0
    for idx in range(1, len(values)):
        delta = float(values[idx]) - float(values[idx - 1])
        if abs(delta) < min_step:
            continue
        sign = 1 if delta > 0 else -1
        if previous_sign and sign != previous_sign:
            flips += 1
        previous_sign = sign
    return flips


def _top_of_book_mid(best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    if best_bid is None or best_ask is None:
        return None
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    return round((float(best_bid) + float(best_ask)) / 2.0, 6)


def _preferred_bid_ask(side: Dict) -> tuple[Optional[float], Optional[float]]:
    exec_bid = _safe_float(side.get("executable_sell"))
    exec_ask = _safe_float(side.get("executable_buy"))
    if exec_bid is not None and exec_ask is not None:
        return min(exec_bid, exec_ask), max(exec_bid, exec_ask)
    return _safe_float(side.get("best_bid")), _safe_float(side.get("best_ask"))


def fetch_external_btc_reference_v1() -> Dict:
    prices: Dict[str, float] = {}

    try:
        res = requests.get(BINANCE_PRICE_URL, params={"symbol": "BTCUSDT"}, timeout=TIMEOUT)
        res.raise_for_status()
        data = res.json() or {}
        value = _safe_float(data.get("price"))
        if value is not None:
            prices["binance"] = value
    except Exception as exc:
        prices["binance_error"] = str(exc)

    try:
        res = requests.get(COINBASE_SPOT_URL, timeout=TIMEOUT)
        res.raise_for_status()
        data = (res.json() or {}).get("data") or {}
        value = _safe_float(data.get("amount"))
        if value is not None:
            prices["coinbase"] = value
    except Exception as exc:
        prices["coinbase_error"] = str(exc)

    source_values = [float(v) for k, v in prices.items() if not k.endswith("_error")]
    reference_price = round(median(source_values), 6) if source_values else None
    source_divergence_bps = None
    if len(source_values) >= 2 and min(source_values) > 0:
        source_divergence_bps = round(((max(source_values) / min(source_values)) - 1.0) * 10000.0, 4)

    return {
        "reference_price": reference_price,
        "source_divergence_bps": source_divergence_bps,
        "sources": prices,
    }


def fetch_binance_open_price_for_event_start_v1(event_start_time: str) -> Dict:
    start_dt = _parse_dt(event_start_time)
    if start_dt is None:
        return {"ok": False, "reason": "invalid_event_start_time", "open_price": None}

    start_ms = int(start_dt.timestamp() * 1000)
    try:
        res = requests.get(
            BINANCE_KLINES_URL,
            params={
                "symbol": "BTCUSDT",
                "interval": "1m",
                "startTime": start_ms,
                "limit": 1,
            },
            timeout=TIMEOUT,
        )
        res.raise_for_status()
        data = res.json() or []
        row = data[0] if data else None
        open_price = _safe_float(row[1]) if row and len(row) > 1 else None
        if open_price is None:
            now_ts = datetime.now(timezone.utc).timestamp()
            if abs(now_ts - start_dt.timestamp()) <= 90:
                fallback = fetch_external_btc_reference_v1()
                fallback_price = _safe_float(fallback.get("reference_price"))
                if fallback_price is not None:
                    return {
                        "ok": True,
                        "reason": "fallback_current_reference_near_open",
                        "open_price": fallback_price,
                    }
            return {"ok": False, "reason": "missing_open_price", "open_price": None}
        return {"ok": True, "reason": "ok", "open_price": open_price}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "open_price": None}


class CurrentScalpResearchV1:
    def __init__(self, cfg: Optional[CurrentScalpConfigV1] = None, history_secs: int = 90):
        self.cfg = cfg or CurrentScalpConfigV1()
        self.history_secs = max(30, int(history_secs))
        self.samples: Deque[_SpotSample] = deque()

    def _trim(self, now_ts: float) -> None:
        cutoff = now_ts - self.history_secs
        while self.samples and self.samples[0].ts < cutoff:
            self.samples.popleft()

    def evaluate(
        self,
        *,
        snap: Dict,
        secs_to_end: Optional[int],
        event_start_time: Optional[str],
        now_ts: float,
        reference_price: Optional[float],
        source_divergence_bps: Optional[float],
        opening_reference_price: Optional[float],
    ) -> Dict:
        up = snap.get("up") or {}
        down = snap.get("down") or {}
        best_up_bid, best_up_ask = _preferred_bid_ask(up)
        best_down_bid, best_down_ask = _preferred_bid_ask(down)

        up_mid = _top_of_book_mid(best_up_bid, best_up_ask)
        down_mid = _top_of_book_mid(best_down_bid, best_down_ask)
        spread_up = round(max(0.0, float(best_up_ask or 0.0) - float(best_up_bid or 0.0)), 6) if best_up_bid is not None and best_up_ask is not None else None
        spread_down = round(max(0.0, float(best_down_ask or 0.0) - float(best_down_bid or 0.0)), 6) if best_down_bid is not None and best_down_ask is not None else None
        combined_bid_depth = round(_sum_depth(up.get("top_bids"), 3) + _sum_depth(down.get("top_bids"), 3), 6)
        combined_ask_depth = round(_sum_depth(up.get("top_asks"), 3) + _sum_depth(down.get("top_asks"), 3), 6)
        combined_depth = round(combined_bid_depth + combined_ask_depth, 6)

        start_dt = _parse_dt(event_start_time)
        elapsed_from_open_secs = None
        if start_dt is not None:
            elapsed_from_open_secs = max(0, int(round(now_ts - start_dt.timestamp())))

        if reference_price is None:
            return {
                "setup": "no_edge",
                "side": None,
                "allow": False,
                "reason": "missing_external_reference_price",
            }
        self._trim(now_ts)

        if up_mid is not None and down_mid is not None:
            self.samples.append(
                _SpotSample(
                    ts=now_ts,
                    reference_price=float(reference_price),
                    up_mid=float(up_mid),
                    up_buy=float(best_up_ask or 0.0),
                    down_buy=float(best_down_ask or 0.0),
                )
            )
            self._trim(now_ts)

        sample_5s = _find_sample_before(self.samples, now_ts, 5)
        sample_15s = _find_sample_before(self.samples, now_ts, 15)
        sample_30s = _find_sample_before(self.samples, now_ts, 30)
        sample_60s = _find_sample_before(self.samples, now_ts, 60)

        spot_delta_5s_bps = _bps_change(reference_price, sample_5s.reference_price if sample_5s else None)
        spot_delta_15s_bps = _bps_change(reference_price, sample_15s.reference_price if sample_15s else None)
        spot_delta_30s_bps = _bps_change(reference_price, sample_30s.reference_price if sample_30s else None)
        spot_delta_60s_bps = _bps_change(reference_price, sample_60s.reference_price if sample_60s else None)
        market_delta_5s = round(float(up_mid) - float(sample_5s.up_mid), 6) if up_mid is not None and sample_5s is not None else None
        market_delta_15s = round(float(up_mid) - float(sample_15s.up_mid), 6) if up_mid is not None and sample_15s is not None else None
        market_range_15s = _range_up_mid(self.samples, now_ts, 15) if up_mid is not None else None
        market_range_30s = _range_up_mid(self.samples, now_ts, 30) if up_mid is not None else None
        market_range_60s = _range_up_mid(self.samples, now_ts, 60) if up_mid is not None else None
        spot_range_60s_usd = _range_reference_price(self.samples, now_ts, 60)
        up_buy_high_60s = _max_buy_price(self.samples, now_ts, 60, "UP")
        down_buy_high_60s = _max_buy_price(self.samples, now_ts, 60, "DOWN")
        distance_from_open_bps = _bps_change(reference_price, opening_reference_price)

        if up_mid is None or down_mid is None:
            return {
                "setup": "no_edge",
                "side": None,
                "allow": False,
                "reason": "missing_market_midpoint",
                "secs_to_end": secs_to_end,
                "elapsed_from_open_secs": elapsed_from_open_secs,
                "reference_price": reference_price,
                "opening_reference_price": opening_reference_price,
                "distance_from_open_bps": distance_from_open_bps,
                "source_divergence_bps": source_divergence_bps,
                "up_mid": up_mid,
                "down_mid": down_mid,
                "up_bid": best_up_bid,
                "up_ask": best_up_ask,
                "down_bid": best_down_bid,
                "down_ask": best_down_ask,
                "spread_up": spread_up,
                "spread_down": spread_down,
                "combined_bid_depth_top3": combined_bid_depth,
                "combined_ask_depth_top3": combined_ask_depth,
                "combined_depth_top3": combined_depth,
                "spot_delta_5s_bps": spot_delta_5s_bps,
                "spot_delta_15s_bps": spot_delta_15s_bps,
                "spot_delta_30s_bps": spot_delta_30s_bps,
                "spot_delta_60s_bps": spot_delta_60s_bps,
                "market_delta_5s": market_delta_5s,
                "market_delta_15s": market_delta_15s,
                "market_range_15s": market_range_15s,
                "market_range_30s": market_range_30s,
                "market_range_60s": market_range_60s,
                "spot_range_60s_usd": spot_range_60s_usd,
                "up_buy_high_60s": up_buy_high_60s,
                "down_buy_high_60s": down_buy_high_60s,
                "target_ticks": self.cfg.target_ticks,
                "stop_ticks": self.cfg.stop_ticks,
                "max_hold_secs": self.cfg.max_hold_secs,
            }

        reasons: List[str] = []
        if secs_to_end is None or secs_to_end < self.cfg.min_secs_to_end:
            reasons.append("too_close_to_expiry")
        if elapsed_from_open_secs is None or elapsed_from_open_secs < self.cfg.min_elapsed_from_open_secs:
            reasons.append("too_early_after_open")
        if spread_up is None or spread_down is None:
            reasons.append("missing_spread")
        elif max(spread_up, spread_down) > self.cfg.max_spread:
            reasons.append(f"spread_too_wide={max(spread_up, spread_down)}")
        if combined_depth < self.cfg.min_depth_top3:
            reasons.append(f"low_combined_depth={combined_depth}")
        if source_divergence_bps is not None and source_divergence_bps > self.cfg.max_source_divergence_bps:
            reasons.append(f"source_divergence_too_high={source_divergence_bps}")
        if distance_from_open_bps is None:
            reasons.append("missing_open_reference_price")
        if sample_15s is None:
            reasons.append("warming_up_need_15s")

        result = {
            "setup": "no_edge",
            "side": None,
            "allow": False,
            "reason": " | ".join(reasons) if reasons else "no_signal",
            "secs_to_end": secs_to_end,
            "elapsed_from_open_secs": elapsed_from_open_secs,
            "reference_price": reference_price,
            "opening_reference_price": opening_reference_price,
            "distance_from_open_bps": distance_from_open_bps,
            "source_divergence_bps": source_divergence_bps,
            "up_mid": up_mid,
            "down_mid": down_mid,
            "up_bid": best_up_bid,
            "up_ask": best_up_ask,
            "down_bid": best_down_bid,
            "down_ask": best_down_ask,
            "spread_up": spread_up,
            "spread_down": spread_down,
            "combined_bid_depth_top3": combined_bid_depth,
            "combined_ask_depth_top3": combined_ask_depth,
            "combined_depth_top3": combined_depth,
            "spot_delta_5s_bps": spot_delta_5s_bps,
            "spot_delta_15s_bps": spot_delta_15s_bps,
            "spot_delta_30s_bps": spot_delta_30s_bps,
            "spot_delta_60s_bps": spot_delta_60s_bps,
            "market_delta_5s": market_delta_5s,
            "market_delta_15s": market_delta_15s,
            "market_range_15s": market_range_15s,
            "market_range_30s": market_range_30s,
            "market_range_60s": market_range_60s,
            "spot_range_60s_usd": spot_range_60s_usd,
            "up_buy_high_60s": up_buy_high_60s,
            "down_buy_high_60s": down_buy_high_60s,
            "target_ticks": self.cfg.target_ticks,
            "stop_ticks": self.cfg.stop_ticks,
            "max_hold_secs": self.cfg.max_hold_secs,
        }

        if reasons:
            return result

        # Continuation setup: the market is already on one side of the open and
        # spot keeps moving in the same direction while Polymarket is not too stretched.
        if (
            distance_from_open_bps is not None
            and distance_from_open_bps >= self.cfg.trend_distance_bps
            and (spot_delta_5s_bps or 0.0) >= self.cfg.continuation_momentum_5s_bps
            and (spot_delta_15s_bps or 0.0) >= self.cfg.continuation_momentum_15s_bps
            and float(up_mid) <= self.cfg.continuation_price_cap
            and (market_delta_5s is None or market_delta_5s <= 0.04)
        ):
            result.update(
                {
                    "setup": "continuation",
                    "side": "UP",
                    "allow": True,
                    "reason": "spot_above_open_and_still_accelerating",
                    "entry_price": best_up_ask,
                    "exit_price": best_up_bid,
                }
            )
            return result

        if (
            distance_from_open_bps is not None
            and distance_from_open_bps <= -self.cfg.trend_distance_bps
            and (spot_delta_5s_bps or 0.0) <= -self.cfg.continuation_momentum_5s_bps
            and (spot_delta_15s_bps or 0.0) <= -self.cfg.continuation_momentum_15s_bps
            and float(down_mid) <= self.cfg.continuation_price_cap
            and (market_delta_5s is None or market_delta_5s >= -0.04)
        ):
            result.update(
                {
                    "setup": "continuation",
                    "side": "DOWN",
                    "allow": True,
                    "reason": "spot_below_open_and_still_accelerating",
                    "entry_price": best_down_ask,
                    "exit_price": best_down_bid,
                }
            )
            return result

        # Reversal setup: spot got extended away from the open but recent
        # short-term momentum turns back while the market is still cheap.
        if (
            distance_from_open_bps is not None
            and distance_from_open_bps <= -self.cfg.reversal_extension_bps
            and (spot_delta_5s_bps or 0.0) >= self.cfg.reversal_bounce_5s_bps
            and (spot_delta_15s_bps or 0.0) >= -self.cfg.reversal_reclaim_15s_bps
            and float(up_mid) <= self.cfg.reversal_price_cap
        ):
            result.update(
                {
                    "setup": "reversal",
                    "side": "UP",
                    "allow": True,
                    "reason": "down_extension_bouncing_back",
                    "entry_price": best_up_ask,
                    "exit_price": best_up_bid,
                }
            )
            return result

        if (
            distance_from_open_bps is not None
            and distance_from_open_bps >= self.cfg.reversal_extension_bps
            and (spot_delta_5s_bps or 0.0) <= -self.cfg.reversal_bounce_5s_bps
            and (spot_delta_15s_bps or 0.0) <= self.cfg.reversal_reclaim_15s_bps
            and float(down_mid) <= self.cfg.reversal_price_cap
        ):
            result.update(
                {
                    "setup": "reversal",
                    "side": "DOWN",
                    "allow": True,
                    "reason": "up_extension_fading_back",
                    "entry_price": best_down_ask,
                    "exit_price": best_down_bid,
                }
            )
            return result

        result["reason"] = "market_not_far_enough_or_not_lagging_enough"
        return result


@dataclass
class CurrentScalpRangeConfigV1:
    min_elapsed_from_open_secs: int = 45
    min_secs_to_end: int = 12
    max_spread: float = 0.03
    min_depth_top3: float = 10.0
    max_source_divergence_bps: float = 6.0
    max_distance_from_open_bps: float = 4.5
    max_spot_drift_30s_bps: float = 3.5
    max_spot_drift_5s_bps: float = 1.5
    min_market_range_15s: float = 0.01
    max_market_range_15s: float = 0.06
    max_market_range_30s: float = 0.12
    min_reversal_flips_15s: int = 0
    entry_zone_low: float = 0.22
    entry_zone_high: float = 0.78
    min_entry_bounce_market_delta_5s: float = 0.005
    max_entry_market_delta_5s: float = 0.03
    min_entry_reversal_spot_5s_bps: float = 0.1
    max_entry_spot_delta_15s_bps: float = 1.0
    max_entry_price: float = 0.62
    target_ticks: int = 1
    stop_ticks: int = 1
    max_hold_secs: int = 12

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class CurrentScalpRangePermissiveConfigV1(CurrentScalpRangeConfigV1):
    max_distance_from_open_bps: float = 6.0
    max_spot_drift_30s_bps: float = 5.0
    max_spot_drift_5s_bps: float = 2.0
    max_market_range_15s: float = 0.075
    max_market_range_30s: float = 0.15
    entry_zone_low: float = 0.28
    entry_zone_high: float = 0.72
    min_entry_bounce_market_delta_5s: float = 0.0
    max_entry_market_delta_5s: float = 0.04
    min_entry_reversal_spot_5s_bps: float = 0.0
    max_entry_spot_delta_15s_bps: float = 1.5
    max_hold_secs: int = 15


@dataclass
class CurrentScalpMicroConfigV1:
    min_elapsed_from_open_secs: int = 45
    min_secs_to_end: int = 18
    max_spread: float = 0.03
    min_depth_top3: float = 10.0
    max_source_divergence_bps: float = 6.0
    max_distance_from_open_bps: float = 8.0
    max_spot_drift_5s_bps: float = 1.8
    max_spot_drift_15s_bps: float = 3.0
    min_side_depth_imbalance: float = 0.18
    min_refill_delta: float = 500.0
    min_counterparty_depletion_delta: float = 250.0
    max_entry_price: float = 0.62
    target_ticks: int = 1
    stop_ticks: int = 1
    max_hold_secs: int = 12

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class CurrentScalpCenterConfigV1:
    min_elapsed_from_open_secs: int = 45
    min_secs_to_end: int = 18
    max_spread: float = 0.03
    min_depth_top3: float = 10.0
    max_source_divergence_bps: float = 6.0
    max_distance_from_open_bps: float = 6.0
    max_spot_drift_5s_bps: float = 1.2
    max_spot_drift_15s_bps: float = 2.0
    min_market_range_15s: float = 0.025
    max_market_range_15s: float = 0.12
    min_reversal_flips_15s: int = 2
    center_price: float = 0.50
    center_band_half_width: float = 0.02
    min_center_deviation: float = 0.03
    max_center_deviation: float = 0.11
    min_bounce_market_delta_5s: float = 0.005
    max_counter_move_15s: float = 0.06
    target_ticks: int = 1
    stop_ticks: int = 1
    max_hold_secs: int = 10

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class CurrentScalpMidMakerConfigV1:
    min_elapsed_from_open_secs: int = 60
    min_secs_to_end: int = 70
    max_spread: float = 0.02
    min_depth_top3: float = 12.0
    max_source_divergence_bps: float = 6.0
    max_distance_from_open_bps: float = 4.0
    max_spot_drift_5s_bps: float = 1.0
    max_spot_drift_15s_bps: float = 1.8
    min_market_range_15s: float = 0.01
    max_market_range_15s: float = 0.05
    min_reversal_flips_15s: int = 3
    center_price: float = 0.50
    min_up_mid: float = 0.45
    max_up_mid: float = 0.55
    max_center_deviation: float = 0.08
    min_center_deviation: float = 0.01
    maker_reentry_delta_5s: float = 0.004
    maker_target_ticks: int = 1
    maker_stop_ticks: int = 1
    maker_max_hold_secs: int = 20

    def as_dict(self) -> Dict:
        return asdict(self)


class CurrentScalpMicroResearchV1:
    def __init__(self, cfg: Optional[CurrentScalpMicroConfigV1] = None, history_secs: int = 90):
        self.cfg = cfg or CurrentScalpMicroConfigV1()
        self.history_secs = max(30, int(history_secs))
        self.samples: Deque[_MicroBookSample] = deque()

    def _trim(self, now_ts: float) -> None:
        cutoff = now_ts - self.history_secs
        while self.samples and self.samples[0].ts < cutoff:
            self.samples.popleft()

    def evaluate(
        self,
        *,
        snap: Dict,
        secs_to_end: Optional[int],
        event_start_time: Optional[str],
        now_ts: float,
        reference_price: Optional[float],
        source_divergence_bps: Optional[float],
        opening_reference_price: Optional[float],
    ) -> Dict:
        up = snap.get("up") or {}
        down = snap.get("down") or {}
        best_up_bid, best_up_ask = _preferred_bid_ask(up)
        best_down_bid, best_down_ask = _preferred_bid_ask(down)
        up_mid = _top_of_book_mid(best_up_bid, best_up_ask)
        down_mid = _top_of_book_mid(best_down_bid, best_down_ask)
        spread_up = round(max(0.0, float(best_up_ask or 0.0) - float(best_up_bid or 0.0)), 6) if best_up_bid is not None and best_up_ask is not None else None
        spread_down = round(max(0.0, float(best_down_ask or 0.0) - float(best_down_bid or 0.0)), 6) if best_down_bid is not None and best_down_ask is not None else None

        up_bid_depth = _sum_depth(up.get("top_bids"), 3)
        up_ask_depth = _sum_depth(up.get("top_asks"), 3)
        down_bid_depth = _sum_depth(down.get("top_bids"), 3)
        down_ask_depth = _sum_depth(down.get("top_asks"), 3)
        combined_depth = round(up_bid_depth + up_ask_depth + down_bid_depth + down_ask_depth, 6)

        start_dt = _parse_dt(event_start_time)
        elapsed_from_open_secs = None
        if start_dt is not None:
            elapsed_from_open_secs = max(0, int(round(now_ts - start_dt.timestamp())))

        if reference_price is None:
            return {"setup": "no_edge", "side": None, "allow": False, "reason": "missing_external_reference_price"}

        self._trim(now_ts)
        if up_mid is not None and down_mid is not None:
            self.samples.append(
                _MicroBookSample(
                    ts=now_ts,
                    reference_price=float(reference_price),
                    up_mid=float(up_mid),
                    up_bid_depth=float(up_bid_depth),
                    up_ask_depth=float(up_ask_depth),
                    down_bid_depth=float(down_bid_depth),
                    down_ask_depth=float(down_ask_depth),
                )
            )
            self._trim(now_ts)

        sample_5s = _find_sample_before(self.samples, now_ts, 5)
        sample_15s = _find_sample_before(self.samples, now_ts, 15)
        spot_delta_5s_bps = _bps_change(reference_price, sample_5s.reference_price if sample_5s else None)
        spot_delta_15s_bps = _bps_change(reference_price, sample_15s.reference_price if sample_15s else None)
        market_delta_5s = round(float(up_mid) - float(sample_5s.up_mid), 6) if up_mid is not None and sample_5s is not None else None
        distance_from_open_bps = _bps_change(reference_price, opening_reference_price)

        up_imbalance = _depth_imbalance(up_bid_depth, up_ask_depth)
        down_imbalance = _depth_imbalance(down_bid_depth, down_ask_depth)
        up_refill_delta = round(up_bid_depth - float(sample_15s.up_bid_depth), 6) if sample_15s is not None else None
        up_depletion_delta = round(float(sample_15s.up_ask_depth) - up_ask_depth, 6) if sample_15s is not None else None
        down_refill_delta = round(down_bid_depth - float(sample_15s.down_bid_depth), 6) if sample_15s is not None else None
        down_depletion_delta = round(float(sample_15s.down_ask_depth) - down_ask_depth, 6) if sample_15s is not None else None

        result = {
            "setup": "no_edge",
            "side": None,
            "allow": False,
            "reason": "no_signal",
            "secs_to_end": secs_to_end,
            "elapsed_from_open_secs": elapsed_from_open_secs,
            "reference_price": reference_price,
            "opening_reference_price": opening_reference_price,
            "distance_from_open_bps": distance_from_open_bps,
            "source_divergence_bps": source_divergence_bps,
            "up_mid": up_mid,
            "down_mid": down_mid,
            "spread_up": spread_up,
            "spread_down": spread_down,
            "combined_depth_top3": combined_depth,
            "spot_delta_5s_bps": spot_delta_5s_bps,
            "spot_delta_15s_bps": spot_delta_15s_bps,
            "market_delta_5s": market_delta_5s,
            "up_bid_depth_top3": up_bid_depth,
            "up_ask_depth_top3": up_ask_depth,
            "down_bid_depth_top3": down_bid_depth,
            "down_ask_depth_top3": down_ask_depth,
            "up_depth_imbalance_top3": up_imbalance,
            "down_depth_imbalance_top3": down_imbalance,
            "up_refill_delta_15s": up_refill_delta,
            "up_depletion_delta_15s": up_depletion_delta,
            "down_refill_delta_15s": down_refill_delta,
            "down_depletion_delta_15s": down_depletion_delta,
            "target_ticks": self.cfg.target_ticks,
            "stop_ticks": self.cfg.stop_ticks,
            "max_hold_secs": self.cfg.max_hold_secs,
        }

        reasons: List[str] = []
        if up_mid is None or down_mid is None:
            reasons.append("missing_market_midpoint")
        if secs_to_end is None or secs_to_end < self.cfg.min_secs_to_end:
            reasons.append("too_close_to_expiry")
        if elapsed_from_open_secs is None or elapsed_from_open_secs < self.cfg.min_elapsed_from_open_secs:
            reasons.append("too_early_after_open")
        if spread_up is None or spread_down is None:
            reasons.append("missing_spread")
        elif max(spread_up, spread_down) > self.cfg.max_spread:
            reasons.append(f"spread_too_wide={max(spread_up, spread_down)}")
        if combined_depth < self.cfg.min_depth_top3:
            reasons.append(f"low_combined_depth={combined_depth}")
        if source_divergence_bps is not None and source_divergence_bps > self.cfg.max_source_divergence_bps:
            reasons.append(f"source_divergence_too_high={source_divergence_bps}")
        if up_mid is not None and not (self.cfg.min_up_mid <= float(up_mid) <= self.cfg.max_up_mid):
            reasons.append(f"up_mid_outside_center_band={up_mid}")
        if distance_from_open_bps is not None and abs(distance_from_open_bps) > self.cfg.max_distance_from_open_bps:
            reasons.append(f"open_distance_too_large={distance_from_open_bps}")
        if sample_15s is None:
            reasons.append("warming_up_need_15s")
        if spot_delta_5s_bps is not None and abs(spot_delta_5s_bps) > self.cfg.max_spot_drift_5s_bps:
            reasons.append(f"spot_drift_5s_too_large={spot_delta_5s_bps}")
        if spot_delta_15s_bps is not None and abs(spot_delta_15s_bps) > self.cfg.max_spot_drift_15s_bps:
            reasons.append(f"spot_drift_15s_too_large={spot_delta_15s_bps}")

        if reasons:
            result["reason"] = " | ".join(reasons)
            return result

        if (
            float(best_up_ask or 0.0) > 0.0
            and float(best_up_ask or 0.0) <= self.cfg.max_entry_price
            and up_imbalance >= self.cfg.min_side_depth_imbalance
            and (up_refill_delta or 0.0) >= self.cfg.min_refill_delta
            and (up_depletion_delta or 0.0) >= self.cfg.min_counterparty_depletion_delta
            and (market_delta_5s or 0.0) >= 0.0
        ):
            result.update(
                {
                    "setup": "micro_imbalance_refill",
                    "side": "UP",
                    "allow": True,
                    "reason": "up_bid_refill_with_ask_depletion",
                    "entry_price": best_up_ask,
                    "exit_price": best_up_bid,
                }
            )
            return result

        if (
            float(best_down_ask or 0.0) > 0.0
            and float(best_down_ask or 0.0) <= self.cfg.max_entry_price
            and down_imbalance >= self.cfg.min_side_depth_imbalance
            and (down_refill_delta or 0.0) >= self.cfg.min_refill_delta
            and (down_depletion_delta or 0.0) >= self.cfg.min_counterparty_depletion_delta
            and (market_delta_5s or 0.0) <= 0.0
        ):
            result.update(
                {
                    "setup": "micro_imbalance_refill",
                    "side": "DOWN",
                    "allow": True,
                    "reason": "down_bid_refill_with_ask_depletion",
                    "entry_price": best_down_ask,
                    "exit_price": best_down_bid,
                }
            )
            return result

        result["reason"] = "no_micro_refill_edge"
        return result


class CurrentScalpCenterResearchV1:
    def __init__(self, cfg: Optional[CurrentScalpCenterConfigV1] = None, history_secs: int = 90):
        self.cfg = cfg or CurrentScalpCenterConfigV1()
        self.history_secs = max(30, int(history_secs))
        self.samples: Deque[_SpotSample] = deque()

    def _trim(self, now_ts: float) -> None:
        cutoff = now_ts - self.history_secs
        while self.samples and self.samples[0].ts < cutoff:
            self.samples.popleft()

    def evaluate(
        self,
        *,
        snap: Dict,
        secs_to_end: Optional[int],
        event_start_time: Optional[str],
        now_ts: float,
        reference_price: Optional[float],
        source_divergence_bps: Optional[float],
        opening_reference_price: Optional[float],
    ) -> Dict:
        up = snap.get("up") or {}
        down = snap.get("down") or {}
        best_up_bid, best_up_ask = _preferred_bid_ask(up)
        best_down_bid, best_down_ask = _preferred_bid_ask(down)
        up_mid = _top_of_book_mid(best_up_bid, best_up_ask)
        down_mid = _top_of_book_mid(best_down_bid, best_down_ask)
        spread_up = round(max(0.0, float(best_up_ask or 0.0) - float(best_up_bid or 0.0)), 6) if best_up_bid is not None and best_up_ask is not None else None
        spread_down = round(max(0.0, float(best_down_ask or 0.0) - float(best_down_bid or 0.0)), 6) if best_down_bid is not None and best_down_ask is not None else None
        combined_depth = round(
            _sum_depth(up.get("top_bids"), 3)
            + _sum_depth(up.get("top_asks"), 3)
            + _sum_depth(down.get("top_bids"), 3)
            + _sum_depth(down.get("top_asks"), 3),
            6,
        )

        start_dt = _parse_dt(event_start_time)
        elapsed_from_open_secs = None
        if start_dt is not None:
            elapsed_from_open_secs = max(0, int(round(now_ts - start_dt.timestamp())))

        if reference_price is None:
            return {"setup": "no_edge", "regime": "unknown", "side": None, "allow": False, "reason": "missing_external_reference_price"}

        self._trim(now_ts)
        if up_mid is not None:
            self.samples.append(_SpotSample(ts=now_ts, reference_price=float(reference_price), up_mid=float(up_mid)))
            self._trim(now_ts)

        sample_5s = _find_sample_before(self.samples, now_ts, 5)
        sample_15s = _find_sample_before(self.samples, now_ts, 15)
        spot_delta_5s_bps = _bps_change(reference_price, sample_5s.reference_price if sample_5s else None)
        spot_delta_15s_bps = _bps_change(reference_price, sample_15s.reference_price if sample_15s else None)
        market_delta_5s = round(float(up_mid) - float(sample_5s.up_mid), 6) if up_mid is not None and sample_5s is not None else None
        market_delta_15s = round(float(up_mid) - float(sample_15s.up_mid), 6) if up_mid is not None and sample_15s is not None else None
        market_range_15s = _range_up_mid(self.samples, now_ts, 15) if up_mid is not None else None
        distance_from_open_bps = _bps_change(reference_price, opening_reference_price)
        window_15s = _window_up_mid_values(self.samples, now_ts, 15)
        reversal_flips_15s = _sign_changes(window_15s)
        center_deviation = round(float(up_mid) - self.cfg.center_price, 6) if up_mid is not None else None
        abs_center_deviation = round(abs(center_deviation), 6) if center_deviation is not None else None
        in_center_band = abs_center_deviation is not None and abs_center_deviation <= self.cfg.center_band_half_width

        result = {
            "setup": "no_edge",
            "regime": "unknown",
            "side": None,
            "allow": False,
            "reason": "no_signal",
            "secs_to_end": secs_to_end,
            "elapsed_from_open_secs": elapsed_from_open_secs,
            "reference_price": reference_price,
            "opening_reference_price": opening_reference_price,
            "distance_from_open_bps": distance_from_open_bps,
            "source_divergence_bps": source_divergence_bps,
            "up_mid": up_mid,
            "down_mid": down_mid,
            "up_bid": best_up_bid,
            "up_ask": best_up_ask,
            "down_bid": best_down_bid,
            "down_ask": best_down_ask,
            "spread_up": spread_up,
            "spread_down": spread_down,
            "combined_depth_top3": combined_depth,
            "spot_delta_5s_bps": spot_delta_5s_bps,
            "spot_delta_15s_bps": spot_delta_15s_bps,
            "market_delta_5s": market_delta_5s,
            "market_delta_15s": market_delta_15s,
            "market_range_15s": market_range_15s,
            "reversal_flips_15s": reversal_flips_15s,
            "center_price": self.cfg.center_price,
            "center_band_half_width": self.cfg.center_band_half_width,
            "center_deviation": center_deviation,
            "abs_center_deviation": abs_center_deviation,
            "in_center_band": in_center_band,
            "target_ticks": self.cfg.target_ticks,
            "stop_ticks": self.cfg.stop_ticks,
            "max_hold_secs": self.cfg.max_hold_secs,
        }

        reasons: List[str] = []
        if up_mid is None or down_mid is None:
            reasons.append("missing_market_midpoint")
        if secs_to_end is None or secs_to_end < self.cfg.min_secs_to_end:
            reasons.append("too_close_to_expiry")
        if elapsed_from_open_secs is None or elapsed_from_open_secs < self.cfg.min_elapsed_from_open_secs:
            reasons.append("too_early_after_open")
        if spread_up is None or spread_down is None:
            reasons.append("missing_spread")
        elif max(spread_up, spread_down) > self.cfg.max_spread:
            reasons.append(f"spread_too_wide={max(spread_up, spread_down)}")
        if combined_depth < self.cfg.min_depth_top3:
            reasons.append(f"low_combined_depth={combined_depth}")
        if source_divergence_bps is not None and source_divergence_bps > self.cfg.max_source_divergence_bps:
            reasons.append(f"source_divergence_too_high={source_divergence_bps}")
        if distance_from_open_bps is not None and abs(distance_from_open_bps) > self.cfg.max_distance_from_open_bps:
            reasons.append(f"open_distance_too_large={distance_from_open_bps}")
        if sample_15s is None:
            reasons.append("warming_up_need_15s")
        if spot_delta_5s_bps is not None and abs(spot_delta_5s_bps) > self.cfg.max_spot_drift_5s_bps:
            reasons.append(f"spot_drift_5s_too_large={spot_delta_5s_bps}")
        if spot_delta_15s_bps is not None and abs(spot_delta_15s_bps) > self.cfg.max_spot_drift_15s_bps:
            reasons.append(f"spot_drift_15s_too_large={spot_delta_15s_bps}")
        if market_range_15s is not None and market_range_15s < self.cfg.min_market_range_15s:
            reasons.append(f"market_range_15s_too_small={market_range_15s}")
        if market_range_15s is not None and market_range_15s > self.cfg.max_market_range_15s:
            reasons.append(f"market_range_15s_too_large={market_range_15s}")
        if reversal_flips_15s < self.cfg.min_reversal_flips_15s:
            reasons.append(f"insufficient_reversal_flips_15s={reversal_flips_15s}")
        if abs_center_deviation is not None and abs_center_deviation < self.cfg.min_center_deviation:
            reasons.append(f"center_deviation_too_small={abs_center_deviation}")
        if abs_center_deviation is not None and abs_center_deviation > self.cfg.max_center_deviation:
            reasons.append(f"center_deviation_too_large={abs_center_deviation}")

        if reasons:
            result["reason"] = " | ".join(reasons)
            return result

        result["regime"] = "center_lateral"
        if center_deviation is not None and center_deviation <= -self.cfg.min_center_deviation:
            if (market_delta_5s or 0.0) >= self.cfg.min_bounce_market_delta_5s and abs(market_delta_15s or 0.0) <= self.cfg.max_counter_move_15s:
                result.update(
                    {
                        "setup": "center_scalp",
                        "side": "UP",
                        "allow": True,
                        "reason": "below_center_reclaiming",
                        "entry_price": best_up_ask,
                        "exit_price": best_up_bid,
                    }
                )
                return result
            result["reason"] = "below_center_without_reclaim"
            return result

        if center_deviation is not None and center_deviation >= self.cfg.min_center_deviation:
            if (market_delta_5s or 0.0) <= -self.cfg.min_bounce_market_delta_5s and abs(market_delta_15s or 0.0) <= self.cfg.max_counter_move_15s:
                result.update(
                    {
                        "setup": "center_scalp",
                        "side": "DOWN",
                        "allow": True,
                        "reason": "above_center_fading",
                        "entry_price": best_down_ask,
                        "exit_price": best_down_bid,
                    }
                )
                return result
            result["reason"] = "above_center_without_fade"
            return result

        result["reason"] = "no_center_edge"
        return result


class CurrentScalpMidMakerResearchV1:
    def __init__(self, cfg: Optional[CurrentScalpMidMakerConfigV1] = None, history_secs: int = 90):
        self.cfg = cfg or CurrentScalpMidMakerConfigV1()
        self.history_secs = max(30, int(history_secs))
        self.samples: Deque[_SpotSample] = deque()

    def _trim(self, now_ts: float) -> None:
        cutoff = now_ts - self.history_secs
        while self.samples and self.samples[0].ts < cutoff:
            self.samples.popleft()

    def evaluate(
        self,
        *,
        snap: Dict,
        secs_to_end: Optional[int],
        event_start_time: Optional[str],
        now_ts: float,
        reference_price: Optional[float],
        source_divergence_bps: Optional[float],
        opening_reference_price: Optional[float],
    ) -> Dict:
        up = snap.get("up") or {}
        down = snap.get("down") or {}
        best_up_bid, best_up_ask = _preferred_bid_ask(up)
        best_down_bid, best_down_ask = _preferred_bid_ask(down)
        up_mid = _top_of_book_mid(best_up_bid, best_up_ask)
        down_mid = _top_of_book_mid(best_down_bid, best_down_ask)
        spread_up = round(max(0.0, float(best_up_ask or 0.0) - float(best_up_bid or 0.0)), 6) if best_up_bid is not None and best_up_ask is not None else None
        spread_down = round(max(0.0, float(best_down_ask or 0.0) - float(best_down_bid or 0.0)), 6) if best_down_bid is not None and best_down_ask is not None else None
        combined_depth = round(
            _sum_depth(up.get("top_bids"), 3)
            + _sum_depth(up.get("top_asks"), 3)
            + _sum_depth(down.get("top_bids"), 3)
            + _sum_depth(down.get("top_asks"), 3),
            6,
        )

        start_dt = _parse_dt(event_start_time)
        elapsed_from_open_secs = None
        if start_dt is not None:
            elapsed_from_open_secs = max(0, int(round(now_ts - start_dt.timestamp())))

        if reference_price is None:
            return {"setup": "no_edge", "regime": "unknown", "side": None, "allow": False, "reason": "missing_external_reference_price"}

        self._trim(now_ts)
        if up_mid is not None:
            self.samples.append(_SpotSample(ts=now_ts, reference_price=float(reference_price), up_mid=float(up_mid)))
            self._trim(now_ts)

        sample_5s = _find_sample_before(self.samples, now_ts, 5)
        sample_15s = _find_sample_before(self.samples, now_ts, 15)
        spot_delta_5s_bps = _bps_change(reference_price, sample_5s.reference_price if sample_5s else None)
        spot_delta_15s_bps = _bps_change(reference_price, sample_15s.reference_price if sample_15s else None)
        market_delta_5s = round(float(up_mid) - float(sample_5s.up_mid), 6) if up_mid is not None and sample_5s is not None else None
        market_range_15s = _range_up_mid(self.samples, now_ts, 15) if up_mid is not None else None
        distance_from_open_bps = _bps_change(reference_price, opening_reference_price)
        window_15s = _window_up_mid_values(self.samples, now_ts, 15)
        reversal_flips_15s = _sign_changes(window_15s)
        center_deviation = round(float(up_mid) - self.cfg.center_price, 6) if up_mid is not None else None
        abs_center_deviation = round(abs(center_deviation), 6) if center_deviation is not None else None

        result = {
            "setup": "no_edge",
            "regime": "unknown",
            "side": None,
            "allow": False,
            "reason": "no_signal",
            "entry_style": "maker_bid",
            "secs_to_end": secs_to_end,
            "elapsed_from_open_secs": elapsed_from_open_secs,
            "reference_price": reference_price,
            "opening_reference_price": opening_reference_price,
            "distance_from_open_bps": distance_from_open_bps,
            "source_divergence_bps": source_divergence_bps,
            "up_mid": up_mid,
            "down_mid": down_mid,
            "up_bid": best_up_bid,
            "up_ask": best_up_ask,
            "down_bid": best_down_bid,
            "down_ask": best_down_ask,
            "spread_up": spread_up,
            "spread_down": spread_down,
            "combined_depth_top3": combined_depth,
            "spot_delta_5s_bps": spot_delta_5s_bps,
            "spot_delta_15s_bps": spot_delta_15s_bps,
            "market_delta_5s": market_delta_5s,
            "market_range_15s": market_range_15s,
            "reversal_flips_15s": reversal_flips_15s,
            "center_deviation": center_deviation,
            "abs_center_deviation": abs_center_deviation,
            "target_ticks": self.cfg.maker_target_ticks,
            "stop_ticks": self.cfg.maker_stop_ticks,
            "max_hold_secs": self.cfg.maker_max_hold_secs,
        }

        reasons: List[str] = []
        if up_mid is None or down_mid is None:
            reasons.append("missing_market_midpoint")
        if secs_to_end is None or secs_to_end < self.cfg.min_secs_to_end:
            reasons.append("too_close_to_expiry")
        if elapsed_from_open_secs is None or elapsed_from_open_secs < self.cfg.min_elapsed_from_open_secs:
            reasons.append("too_early_after_open")
        if spread_up is None or spread_down is None:
            reasons.append("missing_spread")
        elif max(spread_up, spread_down) > self.cfg.max_spread:
            reasons.append(f"spread_too_wide={max(spread_up, spread_down)}")
        if combined_depth < self.cfg.min_depth_top3:
            reasons.append(f"low_combined_depth={combined_depth}")
        if source_divergence_bps is not None and source_divergence_bps > self.cfg.max_source_divergence_bps:
            reasons.append(f"source_divergence_too_high={source_divergence_bps}")
        if distance_from_open_bps is not None and abs(distance_from_open_bps) > self.cfg.max_distance_from_open_bps:
            reasons.append(f"open_distance_too_large={distance_from_open_bps}")
        if sample_15s is None:
            reasons.append("warming_up_need_15s")
        if spot_delta_5s_bps is not None and abs(spot_delta_5s_bps) > self.cfg.max_spot_drift_5s_bps:
            reasons.append(f"spot_drift_5s_too_large={spot_delta_5s_bps}")
        if spot_delta_15s_bps is not None and abs(spot_delta_15s_bps) > self.cfg.max_spot_drift_15s_bps:
            reasons.append(f"spot_drift_15s_too_large={spot_delta_15s_bps}")
        if market_range_15s is not None and market_range_15s < self.cfg.min_market_range_15s:
            reasons.append(f"market_range_15s_too_small={market_range_15s}")
        if market_range_15s is not None and market_range_15s > self.cfg.max_market_range_15s:
            reasons.append(f"market_range_15s_too_large={market_range_15s}")
        if reversal_flips_15s < self.cfg.min_reversal_flips_15s:
            reasons.append(f"not_choppy_enough={reversal_flips_15s}")
        if abs_center_deviation is not None and abs_center_deviation > self.cfg.max_center_deviation:
            reasons.append(f"too_far_from_center={abs_center_deviation}")
        if abs_center_deviation is not None and abs_center_deviation < self.cfg.min_center_deviation:
            reasons.append(f"too_close_to_center={abs_center_deviation}")

        if reasons:
            result["reason"] = " | ".join(reasons)
            return result

        result["regime"] = "mid_book_lateral"
        if center_deviation is not None and center_deviation <= -self.cfg.min_center_deviation and (market_delta_5s or 0.0) >= self.cfg.maker_reentry_delta_5s:
            result.update(
                {
                    "setup": "mid_book_maker",
                    "side": "UP",
                    "allow": True,
                    "reason": "up_reentry_from_discount_near_center",
                    "entry_price": best_up_bid,
                    "exit_price": best_up_ask,
                }
            )
            return result

        if center_deviation is not None and center_deviation >= self.cfg.min_center_deviation and (market_delta_5s or 0.0) <= -self.cfg.maker_reentry_delta_5s:
            result.update(
                {
                    "setup": "mid_book_maker",
                    "side": "DOWN",
                    "allow": True,
                    "reason": "down_reentry_from_discount_near_center",
                    "entry_price": best_down_bid,
                    "exit_price": best_down_ask,
                }
            )
            return result

        result["reason"] = "no_mid_book_reentry"
        return result


class CurrentScalpRangeResearchV1:
    def __init__(self, cfg: Optional[CurrentScalpRangeConfigV1] = None, history_secs: int = 90):
        self.cfg = cfg or CurrentScalpRangeConfigV1()
        self.history_secs = max(30, int(history_secs))
        self.samples: Deque[_SpotSample] = deque()

    def _trim(self, now_ts: float) -> None:
        cutoff = now_ts - self.history_secs
        while self.samples and self.samples[0].ts < cutoff:
            self.samples.popleft()

    def evaluate(
        self,
        *,
        snap: Dict,
        secs_to_end: Optional[int],
        event_start_time: Optional[str],
        now_ts: float,
        reference_price: Optional[float],
        source_divergence_bps: Optional[float],
        opening_reference_price: Optional[float],
    ) -> Dict:
        up = snap.get("up") or {}
        down = snap.get("down") or {}
        best_up_bid, best_up_ask = _preferred_bid_ask(up)
        best_down_bid, best_down_ask = _preferred_bid_ask(down)

        up_mid = _top_of_book_mid(best_up_bid, best_up_ask)
        down_mid = _top_of_book_mid(best_down_bid, best_down_ask)
        spread_up = round(max(0.0, float(best_up_ask or 0.0) - float(best_up_bid or 0.0)), 6) if best_up_bid is not None and best_up_ask is not None else None
        spread_down = round(max(0.0, float(best_down_ask or 0.0) - float(best_down_bid or 0.0)), 6) if best_down_bid is not None and best_down_ask is not None else None
        combined_bid_depth = round(_sum_depth(up.get("top_bids"), 3) + _sum_depth(down.get("top_bids"), 3), 6)
        combined_ask_depth = round(_sum_depth(up.get("top_asks"), 3) + _sum_depth(down.get("top_asks"), 3), 6)
        combined_depth = round(combined_bid_depth + combined_ask_depth, 6)

        start_dt = _parse_dt(event_start_time)
        elapsed_from_open_secs = None
        if start_dt is not None:
            elapsed_from_open_secs = max(0, int(round(now_ts - start_dt.timestamp())))

        if reference_price is None:
            return {
                "setup": "no_edge",
                "regime": "unknown",
                "side": None,
                "allow": False,
                "reason": "missing_external_reference_price",
            }

        self._trim(now_ts)
        if up_mid is not None and down_mid is not None:
            self.samples.append(_SpotSample(ts=now_ts, reference_price=float(reference_price), up_mid=float(up_mid)))
            self._trim(now_ts)

        sample_5s = _find_sample_before(self.samples, now_ts, 5)
        sample_15s = _find_sample_before(self.samples, now_ts, 15)
        sample_30s = _find_sample_before(self.samples, now_ts, 30)

        spot_delta_5s_bps = _bps_change(reference_price, sample_5s.reference_price if sample_5s else None)
        spot_delta_15s_bps = _bps_change(reference_price, sample_15s.reference_price if sample_15s else None)
        spot_delta_30s_bps = _bps_change(reference_price, sample_30s.reference_price if sample_30s else None)
        market_delta_5s = round(float(up_mid) - float(sample_5s.up_mid), 6) if up_mid is not None and sample_5s is not None else None
        market_delta_15s = round(float(up_mid) - float(sample_15s.up_mid), 6) if up_mid is not None and sample_15s is not None else None
        market_range_15s = _range_up_mid(self.samples, now_ts, 15) if up_mid is not None else None
        market_range_30s = _range_up_mid(self.samples, now_ts, 30) if up_mid is not None else None
        distance_from_open_bps = _bps_change(reference_price, opening_reference_price)
        window_15s = _window_up_mid_values(self.samples, now_ts, 15)
        range_position_15s = _range_position(up_mid, window_15s)
        reversal_flips_15s = _sign_changes(window_15s)

        result = {
            "setup": "no_edge",
            "regime": "unknown",
            "side": None,
            "allow": False,
            "reason": "no_signal",
            "secs_to_end": secs_to_end,
            "elapsed_from_open_secs": elapsed_from_open_secs,
            "reference_price": reference_price,
            "opening_reference_price": opening_reference_price,
            "distance_from_open_bps": distance_from_open_bps,
            "source_divergence_bps": source_divergence_bps,
            "up_mid": up_mid,
            "down_mid": down_mid,
            "up_bid": best_up_bid,
            "up_ask": best_up_ask,
            "down_bid": best_down_bid,
            "down_ask": best_down_ask,
            "spread_up": spread_up,
            "spread_down": spread_down,
            "combined_bid_depth_top3": combined_bid_depth,
            "combined_ask_depth_top3": combined_ask_depth,
            "combined_depth_top3": combined_depth,
            "spot_delta_5s_bps": spot_delta_5s_bps,
            "spot_delta_15s_bps": spot_delta_15s_bps,
            "spot_delta_30s_bps": spot_delta_30s_bps,
            "market_delta_5s": market_delta_5s,
            "market_delta_15s": market_delta_15s,
            "market_range_15s": market_range_15s,
            "market_range_30s": market_range_30s,
            "range_position_15s": range_position_15s,
            "reversal_flips_15s": reversal_flips_15s,
            "target_ticks": self.cfg.target_ticks,
            "stop_ticks": self.cfg.stop_ticks,
            "max_hold_secs": self.cfg.max_hold_secs,
        }

        reasons: List[str] = []
        if up_mid is None or down_mid is None:
            reasons.append("missing_market_midpoint")
        if secs_to_end is None or secs_to_end < self.cfg.min_secs_to_end:
            reasons.append("too_close_to_expiry")
        if elapsed_from_open_secs is None or elapsed_from_open_secs < self.cfg.min_elapsed_from_open_secs:
            reasons.append("too_early_after_open")
        if spread_up is None or spread_down is None:
            reasons.append("missing_spread")
        elif max(spread_up, spread_down) > self.cfg.max_spread:
            reasons.append(f"spread_too_wide={max(spread_up, spread_down)}")
        if combined_depth < self.cfg.min_depth_top3:
            reasons.append(f"low_combined_depth={combined_depth}")
        if source_divergence_bps is not None and source_divergence_bps > self.cfg.max_source_divergence_bps:
            reasons.append(f"source_divergence_too_high={source_divergence_bps}")
        if distance_from_open_bps is None:
            reasons.append("missing_open_reference_price")
        if sample_30s is None:
            reasons.append("warming_up_need_30s")

        regime_reasons: List[str] = []
        if distance_from_open_bps is not None and abs(distance_from_open_bps) > self.cfg.max_distance_from_open_bps:
            regime_reasons.append(f"open_distance_too_large={distance_from_open_bps}")
        if spot_delta_30s_bps is not None and abs(spot_delta_30s_bps) > self.cfg.max_spot_drift_30s_bps:
            regime_reasons.append(f"spot_drift_30s_too_large={spot_delta_30s_bps}")
        if spot_delta_5s_bps is not None and abs(spot_delta_5s_bps) > self.cfg.max_spot_drift_5s_bps:
            regime_reasons.append(f"spot_drift_5s_too_large={spot_delta_5s_bps}")
        if market_range_15s is None or market_range_15s < self.cfg.min_market_range_15s:
            regime_reasons.append(f"market_range_15s_too_small={market_range_15s}")
        elif market_range_15s > self.cfg.max_market_range_15s:
            regime_reasons.append(f"market_range_15s_too_large={market_range_15s}")
        if market_range_30s is not None and market_range_30s > self.cfg.max_market_range_30s:
            regime_reasons.append(f"market_range_30s_too_large={market_range_30s}")
        if reversal_flips_15s < self.cfg.min_reversal_flips_15s:
            regime_reasons.append(f"not_choppy_enough={reversal_flips_15s}")

        if reasons:
            result["reason"] = " | ".join(reasons)
            return result

        if regime_reasons:
            result["regime"] = "trending_or_dead"
            result["reason"] = " | ".join(regime_reasons)
            return result

        result["regime"] = "range"

        if (
            range_position_15s is not None
            and range_position_15s <= self.cfg.entry_zone_low
            and (distance_from_open_bps or 0.0) <= 0.0
            and (market_delta_15s or 0.0) <= 0.0
            and (market_delta_5s or 0.0) >= self.cfg.min_entry_bounce_market_delta_5s
            and (market_delta_5s or 0.0) <= self.cfg.max_entry_market_delta_5s
            and (spot_delta_5s_bps or 0.0) >= self.cfg.min_entry_reversal_spot_5s_bps
            and abs(spot_delta_15s_bps or 0.0) <= self.cfg.max_entry_spot_delta_15s_bps
            and float(best_up_ask or 0.0) > 0.0
            and float(best_up_ask or 0.0) <= self.cfg.max_entry_price
        ):
            result.update(
                {
                    "setup": "range_reversion",
                    "side": "UP",
                    "allow": True,
                    "reason": "up_near_local_floor_inside_range",
                    "entry_price": best_up_ask,
                    "exit_price": best_up_bid,
                }
            )
            return result

        if (
            range_position_15s is not None
            and range_position_15s >= self.cfg.entry_zone_high
            and (distance_from_open_bps or 0.0) >= 0.0
            and (market_delta_15s or 0.0) >= 0.0
            and (market_delta_5s or 0.0) <= -self.cfg.min_entry_bounce_market_delta_5s
            and (market_delta_5s or 0.0) >= -self.cfg.max_entry_market_delta_5s
            and (spot_delta_5s_bps or 0.0) <= -self.cfg.min_entry_reversal_spot_5s_bps
            and abs(spot_delta_15s_bps or 0.0) <= self.cfg.max_entry_spot_delta_15s_bps
            and float(best_down_ask or 0.0) > 0.0
            and float(best_down_ask or 0.0) <= self.cfg.max_entry_price
        ):
            result.update(
                {
                    "setup": "range_reversion",
                    "side": "DOWN",
                    "allow": True,
                    "reason": "up_near_local_ceiling_inside_range",
                    "entry_price": best_down_ask,
                    "exit_price": best_down_bid,
                }
            )
            return result

        result["reason"] = "range_ok_but_not_at_local_extreme"
        return result
