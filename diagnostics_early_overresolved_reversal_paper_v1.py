from __future__ import annotations

import argparse
import json
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from market.book_5m import fetch_books_for_tokens, fetch_market_metadata_from_slug
from market.current_scalp_signal_v1 import fetch_binance_open_price_for_event_start_v1, fetch_external_btc_reference_v1
from market.operational_slots_v2 import fetch_operational_slots_v2
from market.public_market_data_v1 import fetch_midpoints
from market.public_market_data_v2 import fetch_token_executable_prices


@dataclass
class EarlyOverresolvedConfigV1:
    qty: int = 50
    leader_price_min: float = 0.88
    leader_price_extreme: float = 0.95
    counter_price_max: float = 0.10
    counter_price_extreme_max: float = 0.06
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


@dataclass
class PaperTrade:
    mode: str = "idle"
    timeframe: str | None = None
    slug: str | None = None
    side: str | None = None
    entry_price: float | None = None
    target_price: float | None = None
    stop_price: float | None = None
    tick_size: float = 0.01
    qty_requested: int = 0
    qty_filled: int = 0
    created_at: float = 0.0
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_ticks: float | None = None
    pnl_usd: float | None = None
    fill_reason: str | None = None


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _default_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"early_overresolved_reversal_paper_{ts}.jsonl"


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _event_start_time_from_item(item: Dict, timeframe: str) -> Optional[str]:
    start_time = item.get("event_start_time") or item.get("startTime")
    if start_time:
        return str(start_time)
    end_dt = _parse_dt(item.get("endDate"))
    if end_dt is None:
        return None
    tf_secs = 300 if timeframe == "5m" else 900
    start_dt = end_dt - timedelta(seconds=tf_secs)
    return start_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug_matches_timeframe(slug: str, timeframe: str) -> bool:
    slug = str(slug or "").lower()
    if timeframe == "5m":
        return "5m" in slug
    if timeframe == "15m":
        return "15m" in slug
    if timeframe == "1h":
        return "1h" in slug or "hour" in slug
    return True


def _make_config(mode: str, qty: int) -> EarlyOverresolvedConfigV1:
    cfg = EarlyOverresolvedConfigV1(qty=max(1, int(qty)))
    if str(mode).lower() != "flex":
        return cfg
    cfg.leader_price_min = 0.70
    cfg.leader_price_extreme = 0.82
    cfg.counter_price_max = 0.30
    cfg.counter_price_extreme_max = 0.22
    cfg.min_secs_to_end_default = 90
    cfg.hard_floor_secs_to_end = 35
    cfg.min_elapsed_default = 12
    cfg.min_elapsed_by_tf_15m = 30
    cfg.min_distance_from_open_bps_5m = 0.9
    cfg.min_distance_from_open_bps_15m = 1.4
    cfg.min_market_range_30s = 0.02
    cfg.min_market_range_60s_15m = 0.03
    cfg.min_spot_reversal_5s_bps = 0.05
    cfg.min_market_pullback_5s = 0.01
    cfg.max_spread_counter = 0.03
    cfg.target_ticks = 1
    cfg.stop_ticks = 1
    cfg.max_hold_secs_5m = 18
    cfg.max_hold_secs_15m = 30
    cfg.late_exception_distance_vs_range = 1.6
    return cfg


def _best_bid(book: Dict) -> Optional[float]:
    bids = book.get("bids") or []
    if not bids:
        return None
    try:
        return float(bids[0]["price"])
    except Exception:
        return None


def _best_ask(book: Dict) -> Optional[float]:
    asks = book.get("asks") or []
    if not asks:
        return None
    try:
        return float(asks[0]["price"])
    except Exception:
        return None


def _raw_book_id(book: Dict) -> str:
    return str(book.get("asset_id") or book.get("token_id") or book.get("id") or "")


def _computed_spread(best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    if best_bid is None or best_ask is None:
        return None
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    return round(best_ask - best_bid, 6)


def _fetch_snap_for_slug(slug: str) -> Tuple[dict, Optional[dict], str]:
    meta = fetch_market_metadata_from_slug(slug)
    if not meta:
        return {"up": None, "down": None}, None, "missing_meta"
    token_mapping = meta.get("token_mapping") or []
    token_ids = [str(x["token_id"]) for x in token_mapping if x.get("token_id")]
    with ThreadPoolExecutor(max_workers=max(2, len(token_ids) + 2)) as pool:
        books_future = pool.submit(fetch_books_for_tokens, token_ids)
        midpoints_future = pool.submit(fetch_midpoints, token_ids)
        executable_futures = {token_id: pool.submit(fetch_token_executable_prices, token_id) for token_id in token_ids}
        raw_books = books_future.result()
        midpoints = midpoints_future.result()
        executable_prices = {token_id: future.result() for token_id, future in executable_futures.items()}
    by_id = {_raw_book_id(book): book for book in raw_books}
    joined = []
    for mapping in token_mapping:
        token_id = str(mapping["token_id"])
        book = by_id.get(token_id) or {}
        best_bid = _best_bid(book)
        best_ask = _best_ask(book)
        midpoint = midpoints.get(token_id)
        spread = _computed_spread(best_bid, best_ask)
        executable = executable_prices.get(token_id) or {}
        joined.append(
            {
                "outcome": mapping.get("outcome"),
                "token_id": token_id,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "midpoint": midpoint,
                "spread": spread,
                "executable_buy": executable.get("BUY"),
                "executable_sell": executable.get("SELL"),
                "tick_size": book.get("tick_size"),
                "top_bids": [{"price": lvl.get("price"), "size": lvl.get("size")} for lvl in (book.get("bids") or [])[:3]],
                "top_asks": [{"price": lvl.get("price"), "size": lvl.get("size")} for lvl in (book.get("asks") or [])[:3]],
            }
        )
    up = None
    down = None
    for item in joined:
        outcome = str(item.get("outcome") or "").lower()
        if outcome == "up":
            up = item
        elif outcome == "down":
            down = item
    snap = {"up": up, "down": down}
    if not up or not down:
        return snap, meta, "missing_up_or_down"
    return snap, meta, "ok"


def _mid_from_side(side_book: dict | None) -> Optional[float]:
    if not side_book:
        return None
    bid = _safe_float(side_book.get("executable_sell"), -1.0)
    ask = _safe_float(side_book.get("executable_buy"), -1.0)
    if bid <= 0 or ask <= 0:
        bid = _safe_float(side_book.get("best_bid"), -1.0)
        ask = _safe_float(side_book.get("best_ask"), -1.0)
    if bid <= 0 or ask <= 0:
        return None
    return round((min(bid, ask) + max(bid, ask)) / 2.0, 6)


def _top_ask_size(side_book: dict | None, ask_price: float) -> float:
    if not side_book:
        return 0.0
    for level in list(side_book.get("top_asks") or [])[:3]:
        level_price = _safe_float(level.get("price"), -1.0)
        if level_price > 0 and abs(level_price - ask_price) < 0.0005:
            return max(0.0, _safe_float(level.get("size"), 0.0))
    levels = list(side_book.get("top_asks") or [])
    if levels:
        return max(0.0, _safe_float(levels[0].get("size"), 0.0))
    return 0.0


def _simulate_aggressive_fill_qty(
    *,
    desired_qty: int,
    side_book: dict | None,
    entry_price: float,
    signal: dict,
) -> tuple[int, str]:
    if desired_qty <= 0 or side_book is None or entry_price <= 0:
        return 0, "invalid_fill_request"
    ask_size = _top_ask_size(side_book, entry_price)
    spread = _safe_float(side_book.get("spread"), 0.01)
    leader_ask = _safe_float(signal.get("leader_ask"), 0.0)
    counter_ask = _safe_float(signal.get("counter_ask"), 0.0)
    overextension = max(0.0, leader_ask - counter_ask)
    fill_factor = 0.65
    if spread <= 0.01:
        fill_factor += 0.15
    if overextension >= 0.50:
        fill_factor += 0.15
    elif overextension >= 0.35:
        fill_factor += 0.08
    simulated_liquidity = ask_size * fill_factor
    filled_qty = int(min(float(desired_qty), simulated_liquidity))
    if filled_qty >= desired_qty:
        return desired_qty, "full_top_book_fill"
    if filled_qty > 0:
        return filled_qty, "partial_top_book_fill_cancel_rest"
    return 0, "no_top_book_liquidity"


def _sum_depth(levels: List[Dict], top_n: int = 3) -> float:
    total = 0.0
    for level in levels[:top_n]:
        total += _safe_float(level.get("size"), 0.0)
    return round(total, 6)


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


class EarlyOverresolvedReversalResearchV1:
    def __init__(self, cfg: Optional[EarlyOverresolvedConfigV1] = None, mode: str = "strict"):
        self.cfg = cfg or EarlyOverresolvedConfigV1()
        self.mode = str(mode).lower()
        self.samples_by_key: Dict[str, Deque[_Sample]] = {}

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
        source_divergence_bps = reference_payload.get("source_divergence_bps")
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
            "source_divergence_bps": source_divergence_bps,
            "up_mid": up_mid,
            "down_mid": down_mid,
            "up_bid": _safe_float(up.get("executable_sell") or up.get("best_bid"), 0.0),
            "up_ask": _safe_float(up.get("executable_buy") or up.get("best_ask"), 0.0),
            "down_bid": _safe_float(down.get("executable_sell") or down.get("best_bid"), 0.0),
            "down_ask": _safe_float(down.get("executable_buy") or down.get("best_ask"), 0.0),
            "distance_from_open_bps": None,
            "spot_delta_5s_bps": None,
            "spot_delta_15s_bps": None,
            "market_delta_5s": None,
            "market_delta_15s": None,
            "market_range_window": None,
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
        event_start_dt = _parse_dt(event_start_time)
        if event_start_dt is not None:
            elapsed_from_open = max(0, int(round((datetime.now(timezone.utc) - event_start_dt).total_seconds())))
        result["elapsed_from_open_secs"] = elapsed_from_open

        min_elapsed = cfg.min_elapsed_by_tf_15m if timeframe == "15m" else cfg.min_elapsed_default
        if elapsed_from_open is not None and elapsed_from_open < min_elapsed:
            result["reason"] = "too_early_after_open"
            return result

        leader_side = "UP" if float(up_mid) >= float(down_mid) else "DOWN"
        leader_bid = result["up_bid"] if leader_side == "UP" else result["down_bid"]
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


def _stats(completed: List[dict]) -> dict:
    total_ticks = round(sum(_safe_float(item.get("pnl_ticks")) for item in completed), 4)
    total_usd = round(sum(_safe_float(item.get("pnl_usd")) for item in completed), 4)
    count = len(completed)
    return {
        "completed_trades": count,
        "wins": sum(1 for item in completed if _safe_float(item.get("pnl_ticks")) > 0),
        "losses": sum(1 for item in completed if _safe_float(item.get("pnl_ticks")) < 0),
        "flat": sum(1 for item in completed if _safe_float(item.get("pnl_ticks")) == 0),
        "total_pnl_ticks": total_ticks,
        "avg_pnl_ticks": round(total_ticks / count, 4) if count else 0.0,
        "total_pnl_usd": total_usd,
        "avg_pnl_usd": round(total_usd / count, 4) if count else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper trade early overresolved reversals on current 5m/15m markets")
    parser.add_argument("--seconds", type=int, default=360, help="Run duration")
    parser.add_argument("--poll-secs", type=float, default=8.0, help="Polling interval")
    parser.add_argument("--timeframes", type=str, default="5m,15m", help="Comma separated timeframes")
    parser.add_argument("--qty", type=int, default=50, help="Paper position size")
    parser.add_argument("--mode", type=str, default="strict", choices=["strict", "flex"], help="Signal strictness")
    parser.add_argument("--log-file", type=str, default=None, help="Optional JSONL log path")
    args = parser.parse_args()

    cfg = _make_config(args.mode, args.qty)
    research = EarlyOverresolvedReversalResearchV1(cfg=cfg, mode=args.mode)
    log_path = Path(args.log_file) if args.log_file else _default_log_path()
    timeframes = [item.strip() for item in str(args.timeframes).split(",") if item.strip()]
    open_refs: Dict[str, Dict[str, Optional[float | str]]] = {}
    trades: Dict[str, PaperTrade] = {tf: PaperTrade() for tf in timeframes}
    completed: List[dict] = []
    blocked = Counter()
    allowed = Counter()
    exits = Counter()

    print("[EARLY_OVERRESOLVED_CONFIG]")
    print(json.dumps(cfg.as_dict(), ensure_ascii=False, indent=2))
    print("[LOG_FILE]")
    print(log_path)

    started_at = time.time()
    slots_cache: Dict[str, Dict] = {}
    slots_refreshed_at = 0.0

    while time.time() - started_at < args.seconds:
        now = time.time()
        if now - slots_refreshed_at >= 20.0 or not slots_cache:
            slots_cache = fetch_operational_slots_v2()
            slots_refreshed_at = now

        reference_payload = fetch_external_btc_reference_v1()
        for timeframe in timeframes:
            current_item = ((slots_cache.get(timeframe) or {}).get("current"))
            if not current_item:
                continue
            slug = str(current_item.get("slug") or "")
            if not _slug_matches_timeframe(slug, timeframe):
                _append_jsonl(log_path, {"type": "snapshot", "ts": now, "timeframe": timeframe, "slug": slug, "reason": "timeframe_slug_mismatch"})
                continue
            event_start_time = _event_start_time_from_item(current_item, timeframe)
            if open_refs.get(timeframe, {}).get("slug") != slug:
                open_payload = fetch_binance_open_price_for_event_start_v1(event_start_time) if event_start_time else {"open_price": None}
                open_refs[timeframe] = {"slug": slug, "price": open_payload.get("open_price"), "event_start_time": event_start_time}

            snap, _meta, snap_reason = _fetch_snap_for_slug(slug)
            if snap_reason != "ok":
                _append_jsonl(log_path, {"type": "snapshot", "ts": now, "timeframe": timeframe, "slug": slug, "reason": snap_reason})
                continue

            secs_to_end = current_item.get("seconds_to_end")
            try:
                secs_to_end = max(0, int(secs_to_end)) if secs_to_end is not None else None
            except Exception:
                secs_to_end = None

            signal = research.evaluate(
                timeframe=timeframe,
                slug=slug,
                snap=snap,
                secs_to_end=secs_to_end,
                event_start_time=str(open_refs.get(timeframe, {}).get("event_start_time") or ""),
                opening_reference_price=_safe_float(open_refs.get(timeframe, {}).get("price"), 0.0) or None,
                reference_payload=reference_payload,
                now_ts=now,
            )

            trade = trades[timeframe]
            tick_size = 0.01
            side = str(signal.get("side") or "")
            side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
            if side_book:
                tick_size = max(0.01, _safe_float(side_book.get("tick_size"), 0.01))

            if trade.mode == "idle" and signal.get("allow"):
                filled_qty, fill_reason = _simulate_aggressive_fill_qty(
                    desired_qty=cfg.qty,
                    side_book=side_book,
                    entry_price=_safe_float(signal.get("entry_price"), 0.0),
                    signal=signal,
                )
                if filled_qty <= 0:
                    blocked[f"{timeframe}|fill:{fill_reason}"] += 1
                    _append_jsonl(
                        log_path,
                        {
                            "type": "blocked_fill",
                            "ts": now,
                            "timeframe": timeframe,
                            "slug": slug,
                            "signal": signal,
                            "fill_reason": fill_reason,
                            "requested_qty": cfg.qty,
                        },
                    )
                else:
                    trade.mode = "open"
                    trade.timeframe = timeframe
                    trade.slug = slug
                    trade.side = side
                    trade.entry_price = _safe_float(signal.get("entry_price"), 0.0)
                    trade.target_price = round(_safe_float(signal.get("target_price"), trade.entry_price + cfg.target_ticks * tick_size), 6)
                    trade.stop_price = round(_safe_float(signal.get("stop_price"), trade.entry_price - cfg.stop_ticks * tick_size), 6)
                    trade.tick_size = tick_size
                    trade.qty_requested = cfg.qty
                    trade.qty_filled = filled_qty
                    trade.created_at = now
                    trade.fill_reason = fill_reason
                    allowed[f"{timeframe}|{signal.get('reason')}|{fill_reason}"] += 1
                    _append_jsonl(log_path, {"type": "enter", "ts": now, "timeframe": timeframe, "slug": slug, "signal": signal, "trade": asdict(trade)})
            elif trade.mode == "idle":
                blocked[f"{timeframe}|{signal.get('reason')}"] += 1

            if trade.mode == "open":
                bid_now = _safe_float(((snap.get("up") if trade.side == "UP" else snap.get("down")) or {}).get("executable_sell"), 0.0)
                if bid_now <= 0:
                    bid_now = _safe_float(((snap.get("up") if trade.side == "UP" else snap.get("down")) or {}).get("best_bid"), 0.0)
                max_hold = cfg.max_hold_secs_15m if timeframe == "15m" else cfg.max_hold_secs_5m
                if bid_now >= _safe_float(trade.target_price):
                    trade.mode = "idle"
                    trade.exit_price = _safe_float(trade.target_price)
                    trade.exit_reason = "target"
                elif bid_now <= _safe_float(trade.stop_price):
                    trade.mode = "idle"
                    trade.exit_price = _safe_float(trade.stop_price)
                    trade.exit_reason = "stop"
                elif secs_to_end is not None and secs_to_end <= cfg.hard_floor_secs_to_end:
                    trade.mode = "idle"
                    trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
                    trade.exit_reason = "late_exit"
                elif now - trade.created_at >= max_hold:
                    trade.mode = "idle"
                    trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
                    trade.exit_reason = "timeout"

                if trade.mode == "idle" and trade.entry_price is not None and trade.exit_price is not None:
                    trade.pnl_ticks = round((trade.exit_price - trade.entry_price) / trade.tick_size, 4)
                    trade.pnl_usd = round((trade.exit_price - trade.entry_price) * trade.qty_filled, 4)
                    exits[f"{timeframe}|{trade.exit_reason}"] += 1
                    completed.append(asdict(trade))
                    _append_jsonl(log_path, {"type": "exit", "ts": now, "timeframe": timeframe, "slug": slug, "signal": signal, "trade": asdict(trade)})
                    trades[timeframe] = PaperTrade()

            _append_jsonl(
                log_path,
                {
                    "type": "snapshot",
                    "ts": now,
                    "timeframe": timeframe,
                    "slug": slug,
                    "secs_to_end": secs_to_end,
                    "signal": signal,
                    "reference": reference_payload,
                    "trade": asdict(trades[timeframe]),
                },
            )

        time.sleep(max(1.0, float(args.poll_secs)))

    summary = {
        "stats": _stats(completed),
        "allowed_reasons": dict(allowed),
        "exit_reasons": dict(exits),
        "top_blocked_reasons": blocked.most_common(12),
        "log_file": str(log_path),
    }
    _append_jsonl(log_path, {"type": "summary", "ts": time.time(), "summary": summary})
    print("[EARLY_OVERRESOLVED_SUMMARY]")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
