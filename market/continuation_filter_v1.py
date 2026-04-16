from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple
import time


@dataclass
class _Sample:
    ts: float
    base_price: float
    bid_depth_top3: float
    ask_depth_top3: float


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


def _oldest_within(samples: Deque[_Sample], now_ts: float, lookback_secs: int) -> Optional[_Sample]:
    cutoff = now_ts - lookback_secs
    candidate = None
    for s in samples:
        if s.ts <= cutoff:
            candidate = s
        else:
            break
    return candidate


def _monotonic_ratio(samples: Deque[_Sample], now_ts: float, lookback_secs: int) -> float:
    cutoff = now_ts - lookback_secs
    recent = [s.base_price for s in samples if s.ts >= cutoff]
    if len(recent) < 4:
        return 0.0
    diffs = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
    non_zero = [d for d in diffs if abs(d) > 1e-9]
    if not non_zero:
        return 0.0
    pos = sum(1 for d in non_zero if d > 0)
    neg = sum(1 for d in non_zero if d < 0)
    major = max(pos, neg)
    return round(major / max(1, len(non_zero)), 4)


def _extract_snapshot_base_price(snap: Dict) -> Optional[float]:
    up = snap.get("up") or {}
    for key in ("display_price", "midpoint", "last_trade_price", "best_bid", "best_ask"):
        v = _safe_float(up.get(key))
        if v is not None:
            return round(v, 6)
    return None


def _extract_combined_depth_top3(snap: Dict) -> Tuple[float, float]:
    up = snap.get("up") or {}
    down = snap.get("down") or {}
    bid_depth = _sum_depth(up.get("top_bids"), 3) + _sum_depth(down.get("top_bids"), 3)
    ask_depth = _sum_depth(up.get("top_asks"), 3) + _sum_depth(down.get("top_asks"), 3)
    return round(bid_depth, 6), round(ask_depth, 6)


class ContinuationRiskFilterV1:
    """
    Lightweight continuation-risk filter for BTC 5m rollouts.

    Output labels:
      - reversal_ok
      - continuation_risk_low
      - continuation_risk_medium
      - continuation_risk_high
    """

    def __init__(self, history_secs: int = 75):
        self.history_secs = max(60, int(history_secs))
        self.by_slot: Dict[str, Deque[_Sample]] = {
            "current": deque(),
            "next_1": deque(),
            "next_2": deque(),
        }

    def _trim(self, slot_name: str, now_ts: float) -> None:
        q = self.by_slot[slot_name]
        cutoff = now_ts - self.history_secs
        while q and q[0].ts < cutoff:
            q.popleft()

    def update_and_classify(self, *, slot_name: str, snap: Dict, now_ts: Optional[float] = None) -> Dict:
        now_ts = float(now_ts or time.time())
        base_price = _extract_snapshot_base_price(snap)
        bid_depth, ask_depth = _extract_combined_depth_top3(snap)

        if base_price is None:
            return {
                "status": "insufficient_data",
                "label": "reversal_ok",
                "score": 0,
                "block_entry": False,
                "reason": "missing_base_price",
            }

        q = self.by_slot.setdefault(slot_name, deque())
        q.append(_Sample(ts=now_ts, base_price=base_price, bid_depth_top3=bid_depth, ask_depth_top3=ask_depth))
        self._trim(slot_name, now_ts)

        s30 = _oldest_within(q, now_ts, 30)
        s60 = _oldest_within(q, now_ts, 60)
        if s30 is None or s60 is None:
            return {
                "status": "warming_up",
                "label": "reversal_ok",
                "score": 0,
                "block_entry": False,
                "reason": "need_60s_history",
                "base_price": base_price,
                "bid_depth_top3": bid_depth,
                "ask_depth_top3": ask_depth,
            }

        delta30 = round(base_price - s30.base_price, 6)
        delta60 = round(base_price - s60.base_price, 6)
        abs_delta30 = abs(delta30)
        abs_delta60 = abs(delta60)
        accel = round(abs_delta30 - abs(delta60 * 0.5), 6)
        mono_ratio = _monotonic_ratio(q, now_ts, 60)

        depth_total = bid_depth + ask_depth
        imbalance = round((bid_depth - ask_depth) / depth_total, 6) if depth_total > 0 else 0.0
        bid_delta30 = round(bid_depth - s30.bid_depth_top3, 6)
        ask_delta30 = round(ask_depth - s30.ask_depth_top3, 6)

        trend_dir = 1 if delta30 > 0 else (-1 if delta30 < 0 else 0)
        directional_imbalance = (trend_dir > 0 and imbalance >= 0.20) or (trend_dir < 0 and imbalance <= -0.20)
        refill_depletion = (trend_dir > 0 and ask_delta30 <= -2.0 and bid_delta30 >= 0.0) or (
            trend_dir < 0 and bid_delta30 <= -2.0 and ask_delta30 >= 0.0
        )

        score = 0
        reasons: List[str] = []
        if abs_delta30 >= 0.03:
            score += 1
            reasons.append("delta30_strong")
        if abs_delta60 >= 0.05:
            score += 1
            reasons.append("delta60_strong")
        if mono_ratio >= 0.80:
            score += 1
            reasons.append("monotonic_move")
        if directional_imbalance:
            score += 1
            reasons.append("directional_book_imbalance_top3")
        if refill_depletion:
            score += 1
            reasons.append("depletion_without_refill")
        if abs_delta30 >= 0.05 or accel >= 0.02:
            score += 1
            reasons.append("volatility_burst")

        if score >= 4:
            label = "continuation_risk_high"
        elif score == 3:
            label = "continuation_risk_medium"
        elif score == 2:
            label = "continuation_risk_low"
        else:
            label = "reversal_ok"

        block_entry = label == "continuation_risk_high"
        return {
            "status": "ok",
            "label": label,
            "score": score,
            "block_entry": block_entry,
            "reasons": reasons,
            "base_price": base_price,
            "delta30": delta30,
            "delta60": delta60,
            "abs_delta30": abs_delta30,
            "abs_delta60": abs_delta60,
            "accel": accel,
            "monotonic_ratio_60s": mono_ratio,
            "bid_depth_top3": bid_depth,
            "ask_depth_top3": ask_depth,
            "depth_imbalance_top3": imbalance,
            "bid_depth_delta30": bid_delta30,
            "ask_depth_delta30": ask_delta30,
        }
