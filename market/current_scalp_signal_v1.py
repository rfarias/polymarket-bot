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
    up_buy: float
    down_buy: float


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
