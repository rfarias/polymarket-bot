from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from market.book_5m import fetch_books_for_tokens
from market.broker_env import load_broker_env
from market.broker_types import BrokerOrderRequest
from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.live_guarded_config import load_live_guarded_config
from market.polymarket_broker_v3 import PolymarketBrokerV3
from market.slug_discovery import fetch_event_by_slug


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


FIVE_MINUTE_STEP = 300


def _build_log_dir() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"current_almost_resolved_real_{ts}"


def _state_path() -> Path:
    return Path("logs") / "current_almost_resolved_real_state.json"


def _residuals_path() -> Path:
    return Path("logs") / "current_almost_resolved_real_residuals.jsonl"


def _save_state(path: Path, trade) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(trade), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state(path: Path):
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return LiveCurrentAlmostResolvedTradeState(**payload)
    except Exception:
        return None


def _clear_state(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


@dataclass
class LiveCurrentAlmostResolvedTradeState:
    mode: str = "idle"  # idle | pending_entry | open_position | pending_exit | exit_pending_confirm
    event_slug: Optional[str] = None
    side: Optional[str] = None
    token_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    entry_price: Optional[float] = None
    entry_qty_requested: float = 0.0
    entry_qty_filled: float = 0.0
    exit_qty_filled: float = 0.0
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    best_bid: Optional[float] = None
    hold_to_resolution: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    confirm_started_at: float = 0.0
    confirm_polls: int = 0
    entry_reprice_count: int = 0
    last_reason: Optional[str] = None

    @property
    def remaining_position_qty(self) -> float:
        return round(max(0.0, float(self.entry_qty_filled) - float(self.exit_qty_filled)), 6)


@dataclass
class CurrentSlotCache:
    slug: Optional[str] = None
    item: Optional[dict] = None
    snap: Optional[dict] = None
    event_start_time: Optional[str] = None
    opening_reference_price: Optional[float] = None
    reference_cache_ts: float = 0.0
    reference_cache_payload: Optional[dict] = None


def _trade_summary(trade: LiveCurrentAlmostResolvedTradeState) -> dict:
    return asdict(trade)


def _parse_dt(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _round_down_to_current_5m_epoch(now_ts: int) -> int:
    return (now_ts // FIVE_MINUTE_STEP) * FIVE_MINUTE_STEP


def _normalize_current_event(event: dict) -> Optional[dict]:
    if not event:
        return None
    slug = str(event.get("slug") or "")
    if not slug.startswith("btc-updown-5m-"):
        return None
    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    if market.get("active") is not True:
        return None
    if market.get("closed") is True:
        return None
    if market.get("acceptingOrders") is not True:
        return None
    if market.get("enableOrderBook") is not True:
        return None
    end_dt = _parse_dt(event.get("endDate") or market.get("endDate"))
    if not end_dt:
        return None
    now = datetime.now(timezone.utc)
    secs_to_end = int(round((end_dt - now).total_seconds()))
    if secs_to_end <= 0:
        return None
    raw_token_ids = market.get("clobTokenIds") or []
    raw_outcomes = market.get("outcomes") or []
    try:
        token_ids = raw_token_ids if isinstance(raw_token_ids, list) else json.loads(raw_token_ids)
    except Exception:
        token_ids = []
    try:
        outcomes = raw_outcomes if isinstance(raw_outcomes, list) else json.loads(raw_outcomes)
    except Exception:
        outcomes = []
    token_mapping = []
    for idx, token_id in enumerate(token_ids):
        outcome = str(outcomes[idx] if idx < len(outcomes) else f"OUTCOME_{idx}")
        token_mapping.append({"outcome": outcome, "token_id": str(token_id)})
    return {
        "title": event.get("title"),
        "slug": slug,
        "market_slug": market.get("slug"),
        "seconds_to_end": secs_to_end,
        "endDate": event.get("endDate") or market.get("endDate"),
        "event_start_time": market.get("eventStartTime") or event.get("startTime"),
        "token_mapping": token_mapping,
    }


def _build_current_snap_from_event(event: dict) -> tuple[Optional[dict], Optional[dict]]:
    item = _normalize_current_event(event)
    if not item:
        return None, None
    token_ids = [str(x.get("token_id")) for x in (item.get("token_mapping") or []) if x.get("token_id")]
    raw_books = fetch_books_for_tokens(token_ids)
    by_id = {str(book.get("asset_id") or book.get("token_id") or ""): book for book in raw_books}

    def _side_payload(mapping: dict) -> Optional[dict]:
        token_id = str(mapping.get("token_id") or "")
        book = by_id.get(token_id) or {}
        if not book:
            return None
        top_bids = [{"price": lvl.get("price"), "size": lvl.get("size")} for lvl in (book.get("bids") or [])[:3]]
        top_asks = [{"price": lvl.get("price"), "size": lvl.get("size")} for lvl in (book.get("asks") or [])[:3]]
        return {
            "outcome": mapping.get("outcome"),
            "token_id": token_id,
            "best_bid": _best_bid(book),
            "best_ask": _safe_float(((book.get("asks") or [{}])[0] or {}).get("price")),
            "executable_buy": _safe_float(((book.get("asks") or [{}])[0] or {}).get("price")),
            "executable_sell": _best_bid(book),
            "tick_size": _safe_float(book.get("tick_size"), 0.01),
            "min_order_size": _safe_float(book.get("min_order_size"), 0.0),
            "top_bids": top_bids,
            "top_asks": top_asks,
        }

    up = None
    down = None
    for mapping in item.get("token_mapping") or []:
        outcome = str(mapping.get("outcome") or "").lower()
        if outcome == "up":
            up = _side_payload(mapping)
        elif outcome == "down":
            down = _side_payload(mapping)
    if not up or not down:
        return item, None
    return item, {"up": up, "down": down}


def _fetch_current_item_and_snap(cache: CurrentSlotCache) -> tuple[Optional[dict], Optional[dict], str]:
    target_slug = f"btc-updown-5m-{_round_down_to_current_5m_epoch(int(datetime.now(timezone.utc).timestamp()))}"
    if cache.slug != target_slug or cache.item is None:
        raw_event = fetch_event_by_slug(target_slug)
        item, snap = _build_current_snap_from_event(raw_event or {})
        cache.slug = target_slug
        cache.item = item
        cache.snap = snap
        cache.event_start_time = item.get("event_start_time") if item else None
        cache.opening_reference_price = None
        if item and item.get("event_start_time"):
            open_ref = fetch_binance_open_price_for_event_start_v1(str(item.get("event_start_time")))
            cache.opening_reference_price = _safe_float(open_ref.get("open_price"))
        return item, snap, "ok" if item and snap else "missing_current"
    item = dict(cache.item or {})
    end_dt = _parse_dt(item.get("endDate"))
    if end_dt is not None:
        item["seconds_to_end"] = max(0, int(round((end_dt - datetime.now(timezone.utc)).total_seconds())))
    token_ids = [str(x.get("token_id")) for x in (item.get("token_mapping") or []) if x.get("token_id")]
    raw_books = fetch_books_for_tokens(token_ids)
    by_id = {str(book.get("asset_id") or book.get("token_id") or ""): book for book in raw_books}

    def _from_book(mapping: dict) -> Optional[dict]:
        token_id = str(mapping.get("token_id") or "")
        book = by_id.get(token_id) or {}
        if not book:
            return None
        return {
            "outcome": mapping.get("outcome"),
            "token_id": token_id,
            "best_bid": _best_bid(book),
            "best_ask": _safe_float(((book.get("asks") or [{}])[0] or {}).get("price")),
            "executable_buy": _safe_float(((book.get("asks") or [{}])[0] or {}).get("price")),
            "executable_sell": _best_bid(book),
            "tick_size": _safe_float(book.get("tick_size"), 0.01),
            "min_order_size": _safe_float(book.get("min_order_size"), 0.0),
            "top_bids": [{"price": lvl.get("price"), "size": lvl.get("size")} for lvl in (book.get("bids") or [])[:3]],
            "top_asks": [{"price": lvl.get("price"), "size": lvl.get("size")} for lvl in (book.get("asks") or [])[:3]],
        }

    up = None
    down = None
    for mapping in item.get("token_mapping") or []:
        outcome = str(mapping.get("outcome") or "").lower()
        if outcome == "up":
            up = _from_book(mapping)
        elif outcome == "down":
            down = _from_book(mapping)
    snap = {"up": up, "down": down} if up and down else None
    cache.item = item
    cache.snap = snap
    return item, snap, "ok" if item and snap else "missing_current"


def _cached_external_reference(cache: CurrentSlotCache, *, ttl_secs: float) -> dict:
    now = time.time()
    if cache.reference_cache_payload is not None and now - cache.reference_cache_ts <= ttl_secs:
        return cache.reference_cache_payload
    payload = fetch_external_btc_reference_v1()
    cache.reference_cache_payload = payload
    cache.reference_cache_ts = now
    return payload


def _tick_size_from_snap(snap: dict, side: str) -> float:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(side_book.get("tick_size"), 0.01))


def _token_id_for_side(snap: dict, side: str) -> str:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return str(side_book.get("token_id") or "")


def _bid_for_side(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _fetch_active_book(trade: LiveCurrentAlmostResolvedTradeState) -> Optional[dict]:
    if not trade.token_id:
        return None
    raw_books = fetch_books_for_tokens([trade.token_id])
    if not raw_books:
        return None
    return raw_books[0]


def _best_bid(book: dict) -> float:
    bids = book.get("bids") or []
    if not bids:
        return 0.0
    return _safe_float((bids[0] or {}).get("price"), 0.0)


def _get_order_status(broker, order_id: Optional[str]):
    if not order_id:
        return None
    try:
        order = broker.get_order(order_id)
        if order is not None:
            return order
    except Exception:
        pass
    try:
        for order in broker.get_open_orders()[:50]:
            if order.order_id == order_id:
                return order
    except Exception:
        pass
    return None


def _cancel_if_live(broker, order_id: Optional[str]) -> Optional[dict]:
    if not order_id:
        return None
    order = _get_order_status(broker, order_id)
    status = str(getattr(order, "status", "") or "").lower()
    if status in ("filled", "canceled", "cancelled", "closed", "resolved", "rejected"):
        return None
    return broker.cancel_order(order_id)


def _token_balance_qty(broker, token_id: Optional[str]) -> float:
    if not token_id:
        return 0.0
    try:
        payload = broker.get_balance_allowance(asset_type="CONDITIONAL", token_id=token_id)
        raw_balance = float(payload.get("balance") or 0.0)
        return round(raw_balance / 1_000_000.0, 6)
    except Exception:
        return 0.0


def _collateral_balance_usd(broker) -> float:
    try:
        payload = broker.get_balance_allowance(asset_type="COLLATERAL")
        raw_balance = float(payload.get("balance") or 0.0)
        return round(raw_balance / 1_000_000.0, 6)
    except Exception:
        return 0.0


def _is_flat_qty(qty: float, epsilon: float = 0.000001) -> bool:
    return abs(float(qty)) <= float(epsilon)


def _is_match_status(status: Optional[str]) -> bool:
    return str(status or "").lower() in ("matched", "filled", "closed", "resolved")


def _has_sufficient_collateral_for_entry(broker, *, entry_price: float, qty: float, buffer_usd: float = 0.25) -> bool:
    required = round(float(entry_price) * float(qty) + float(buffer_usd), 6)
    return _collateral_balance_usd(broker) >= required


def _effective_entry_price(
    *,
    signal_entry_price: float,
    target_exit_price: float,
    tick_size: float,
    premium_ticks: int,
    min_profit_ticks_after_entry: int,
) -> float:
    base_price = max(0.01, float(signal_entry_price))
    if premium_ticks <= 0 or tick_size <= 0:
        return round(base_price, 6)
    max_price_for_profit = max(
        0.01,
        float(target_exit_price) - max(1, int(min_profit_ticks_after_entry)) * float(tick_size),
    )
    bumped_price = base_price + int(premium_ticks) * float(tick_size)
    return round(min(base_price if max_price_for_profit < base_price else bumped_price, max_price_for_profit), 6)


def _reprice_entry_price(
    *,
    current_entry_price: float,
    signal_entry_price: float,
    target_exit_price: float,
    tick_size: float,
    reprice_ticks: int,
    min_profit_ticks_after_entry: int,
) -> float:
    higher_base = max(float(current_entry_price), float(signal_entry_price)) + max(1, int(reprice_ticks)) * float(tick_size)
    return _effective_entry_price(
        signal_entry_price=higher_base,
        target_exit_price=target_exit_price,
        tick_size=tick_size,
        premium_ticks=0,
        min_profit_ticks_after_entry=min_profit_ticks_after_entry,
    )


def _entry_execution_plan(
    *,
    signal: dict,
    tick_size: float,
    default_premium_ticks: int,
    default_timeout_secs: float,
    min_profit_ticks_after_entry: int,
) -> dict:
    secs = _safe_float(signal.get("secs_to_end"), 0.0)
    reason = str(signal.get("reason") or "")
    entry_price = _safe_float(signal.get("entry_price"), 0.0)
    exit_price = _safe_float(signal.get("exit_price"), 0.99)
    dist_usd = _safe_float(signal.get("distance_to_price_to_beat_usd"), 0.0)
    range30 = _safe_float(signal.get("market_range_30s"), 0.0)

    premium_ticks = int(default_premium_ticks)
    timeout_secs = float(default_timeout_secs)

    if reason in ("leader_up_dual_rich_late_limit", "leader_down_dual_rich_late_limit"):
        premium_ticks = 0
        timeout_secs = max(timeout_secs, 2.5 if secs >= 20 else 1.5)
        target_limit_price = _safe_float(signal.get("target_limit_price"), 0.98)
        effective_entry = min(max(0.01, target_limit_price), max(entry_price, 0.01))
        return {
            "premium_ticks": premium_ticks,
            "timeout_secs": timeout_secs,
            "entry_price": round(effective_entry, 6),
        }
    if reason in ("leader_up_extreme_dominance", "leader_down_extreme_dominance"):
        premium_ticks = max(premium_ticks, 2)
        timeout_secs = max(timeout_secs, 3.0)
    elif dist_usd >= 90 and range30 <= 0.02 and secs >= 35:
        premium_ticks = max(premium_ticks, 2)
        timeout_secs = max(timeout_secs, 2.5)
    elif secs <= 30 and dist_usd >= 55:
        premium_ticks = max(premium_ticks, 2)
        timeout_secs = max(timeout_secs, 1.5)

    if exit_price - entry_price <= max(1, min_profit_ticks_after_entry) * tick_size:
        premium_ticks = min(premium_ticks, 1)

    effective_entry = _effective_entry_price(
        signal_entry_price=entry_price,
        target_exit_price=exit_price,
        tick_size=tick_size,
        premium_ticks=premium_ticks,
        min_profit_ticks_after_entry=min_profit_ticks_after_entry,
    )
    return {
        "premium_ticks": premium_ticks,
        "timeout_secs": timeout_secs,
        "entry_price": effective_entry,
    }


def _maybe_reprice_pending_entry(
    broker,
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    signal: dict,
    snap: Optional[dict],
    qty: int,
    now: float,
    cfg: CurrentAlmostResolvedConfigV1,
    min_profit_ticks_after_entry: int,
    reprice_ticks: int,
) -> tuple[LiveCurrentAlmostResolvedTradeState, Optional[dict]]:
    if trade.mode != "pending_entry" or trade.entry_reprice_count > 0 or not snap:
        return trade, None
    if not signal.get("allow") or str(signal.get("side") or "") != str(trade.side or ""):
        return trade, None
    if str(signal.get("event_slug") or "") != str(trade.event_slug or ""):
        return trade, None

    tick_size = _tick_size_from_snap(snap, trade.side or "UP")
    current_entry_price = _safe_float(trade.entry_price, 0.0)
    target_exit_price = _safe_float(trade.target_price, cfg.target_exit_price)
    repriced_entry = _reprice_entry_price(
        current_entry_price=current_entry_price,
        signal_entry_price=_safe_float(signal.get("entry_price"), current_entry_price),
        target_exit_price=target_exit_price,
        tick_size=tick_size,
        reprice_ticks=reprice_ticks,
        min_profit_ticks_after_entry=min_profit_ticks_after_entry,
    )
    if repriced_entry <= current_entry_price:
        return trade, None
    if not _has_sufficient_collateral_for_entry(broker, entry_price=repriced_entry, qty=qty):
        return trade, {
            "status": "blocked_insufficient_collateral",
            "current_entry_price": current_entry_price,
            "repriced_entry_price": repriced_entry,
            "available": _collateral_balance_usd(broker),
        }

    req = BrokerOrderRequest(
        token_id=trade.token_id or "",
        side="BUY",
        price=repriced_entry,
        size=float(qty),
        market_slug=trade.event_slug,
        outcome=trade.side,
        client_order_key=f"current_almost_resolved:entry_reprice:{int(now)}:{trade.side}",
    )
    order = broker.place_limit_order(req)
    trade.entry_order_id = order.order_id
    trade.entry_price = repriced_entry
    trade.entry_reprice_count += 1
    trade.updated_at = now
    trade.stop_price = round(max(0.01, repriced_entry - cfg.stop_ticks * tick_size), 6)
    trade.last_reason = "entry_repriced"
    return trade, {
        "status": "repriced",
        "current_entry_price": current_entry_price,
        "repriced_entry_price": repriced_entry,
        "reprice_ticks": reprice_ticks,
    }


def _is_aggressive_exit_reason(reason: str) -> bool:
    return str(reason or "").lower() in {
        "stop",
        "structural_stop",
        "deadline_flatten",
        "timeout",
        "repost",
        "retry_residual",
        "shutdown_flatten",
        "panic",
    }


def _sync_entry_order(broker, trade: LiveCurrentAlmostResolvedTradeState) -> LiveCurrentAlmostResolvedTradeState:
    order = _get_order_status(broker, trade.entry_order_id)
    if order is not None:
        trade.entry_qty_filled = max(trade.entry_qty_filled, _safe_float(getattr(order, "size_matched", None), 0.0))
    token_balance = _token_balance_qty(broker, trade.token_id)
    if token_balance > 0:
        trade.entry_qty_filled = max(trade.entry_qty_filled, token_balance + float(trade.exit_qty_filled))
    return trade


def _restore_trade_from_broker(broker, trade: LiveCurrentAlmostResolvedTradeState) -> LiveCurrentAlmostResolvedTradeState:
    if trade.mode == "idle":
        return trade
    trade = _sync_entry_order(broker, trade)
    exit_order = _get_order_status(broker, trade.exit_order_id)
    if exit_order is not None:
        trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
        status = str(getattr(exit_order, "status", "") or "").lower()
        if _is_flat_qty(_token_balance_qty(broker, trade.token_id)) or trade.remaining_position_qty <= 0 or _is_match_status(status):
            return LiveCurrentAlmostResolvedTradeState()
        trade.mode = "pending_exit"
    if trade.entry_qty_filled > 0 and trade.mode == "pending_entry":
        trade.mode = "open_position"
    return trade


def _should_hold_to_resolution(signal: dict, *, bid_now: float, secs_to_end: Optional[int], cfg: CurrentAlmostResolvedConfigV1, side: str) -> bool:
    buffer_bps = _safe_float(signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"), 0.0)
    open_distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    market_range_30s = _safe_float(signal.get("market_range_30s"), 0.0)
    return (
        secs_to_end is not None
        and secs_to_end <= cfg.paper_hold_to_resolution_secs
        and bid_now >= cfg.paper_hold_to_resolution_min_price
        and buffer_bps >= cfg.paper_hold_to_resolution_min_buffer_bps
        and open_distance_bps >= cfg.paper_hold_to_resolution_min_open_distance_bps
        and market_range_30s <= cfg.paper_profit_take_on_market_range_30s
    )


def _exit_reason(
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    bid_now: float,
    tick_size: float,
    now: float,
    secs_to_end: Optional[int],
    signal: dict,
    cfg: CurrentAlmostResolvedConfigV1,
    flatten_deadline_secs: int,
) -> Optional[str]:
    if bid_now <= 0 or trade.entry_price is None:
        return None
    side = trade.side or "UP"
    trade.best_bid = max(_safe_float(trade.best_bid), bid_now)
    pnl_ticks_now = (bid_now - float(trade.entry_price)) / tick_size if tick_size > 0 else 0.0
    buffer_bps = _safe_float(signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"), 0.0)
    open_distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    market_range_30s = _safe_float(signal.get("market_range_30s"), 0.0)
    edge_vs_counter = _safe_float(signal.get("up_edge_vs_counter" if side == "UP" else "down_edge_vs_counter"), 0.0)
    adverse_spot_bps = _safe_float(signal.get("up_adverse_spot_bps" if side == "UP" else "down_adverse_spot_bps"), 0.0)

    if secs_to_end is not None and secs_to_end <= flatten_deadline_secs:
        return "deadline_flatten"
    if bid_now >= _safe_float(trade.target_price):
        return "target"
    if bid_now <= _safe_float(trade.stop_price):
        return "stop"
    if (
        pnl_ticks_now >= cfg.paper_profit_take_min_ticks
        and (
            (secs_to_end is not None and secs_to_end <= cfg.paper_profit_take_late_secs)
            or buffer_bps <= cfg.paper_profit_take_on_reversal_buffer_bps
            or market_range_30s >= cfg.paper_profit_take_on_market_range_30s
            or adverse_spot_bps >= open_distance_bps * cfg.max_reversal_share_of_open_distance
        )
    ):
        return "profit_protect"
    if pnl_ticks_now > 0 and not trade.hold_to_resolution and secs_to_end is not None and secs_to_end <= cfg.paper_hold_to_resolution_secs:
        return "late_profit_take"
    if (
        buffer_bps <= cfg.paper_structural_stop_buffer_bps
        or market_range_30s >= cfg.paper_structural_stop_market_range_30s
        or edge_vs_counter <= cfg.paper_structural_stop_edge_vs_counter
        or (signal.get("side") not in (None, side) and signal.get("allow"))
    ):
        return "structural_stop"
    if not trade.hold_to_resolution and now - trade.created_at >= cfg.max_hold_secs:
        return "timeout"
    return None


def _post_entry_order(
    broker,
    *,
    signal: dict,
    snap: dict,
    qty: int,
    tick_size: float,
    now: float,
    cfg: CurrentAlmostResolvedConfigV1,
    execution_plan: dict,
) -> LiveCurrentAlmostResolvedTradeState:
    side = str(signal.get("side") or "")
    entry_price = _safe_float(execution_plan.get("entry_price"), 0.0)
    trade = LiveCurrentAlmostResolvedTradeState(
        mode="pending_entry",
        event_slug=str(signal.get("event_slug") or ""),
        side=side,
        token_id=_token_id_for_side(snap, side),
        entry_price=entry_price,
        entry_qty_requested=float(qty),
        target_price=round(min(0.99, _safe_float(signal.get("exit_price"), cfg.target_exit_price)), 6),
        stop_price=round(max(0.01, entry_price - cfg.stop_ticks * tick_size), 6),
        created_at=now,
        updated_at=now,
        last_reason="entry_posted",
    )
    if not trade.token_id:
        raise RuntimeError(f"Missing token_id for side={side}")
    if qty < 6:
        raise RuntimeError("Current almost resolved real requires qty >= 6.")
    if not _has_sufficient_collateral_for_entry(broker, entry_price=entry_price, qty=qty):
        raise RuntimeError(
            f"Insufficient collateral for entry: required={round(entry_price * qty, 6)} available={_collateral_balance_usd(broker)}"
        )
    req = BrokerOrderRequest(
        token_id=trade.token_id,
        side="BUY",
        price=entry_price,
        size=float(qty),
        market_slug=trade.event_slug,
        outcome=side,
        client_order_key=f"current_almost_resolved:entry:{int(now)}:{side}",
    )
    order = broker.place_limit_order(req)
    trade.entry_order_id = order.order_id
    return trade


def _post_exit_order(
    broker,
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    exit_price: float,
    now: float,
    reason: str,
    min_limit_exit_qty: float,
) -> LiveCurrentAlmostResolvedTradeState:
    token_balance_qty = _token_balance_qty(broker, trade.token_id)
    qty = token_balance_qty if token_balance_qty > 0 else trade.remaining_position_qty
    aggressive_exit = _is_aggressive_exit_reason(reason)
    if _is_flat_qty(qty):
        trade.mode = "idle"
        trade.last_reason = "flat"
        trade.updated_at = now
        return trade
    try:
        broker.update_balance_allowance(asset_type="CONDITIONAL", token_id=trade.token_id)
    except Exception:
        pass
    if (aggressive_exit or qty < float(min_limit_exit_qty)) and hasattr(broker, "place_market_order"):
        try:
            order = broker.place_market_order(
                token_id=trade.token_id or "",
                side="SELL",
                amount=float(qty),
                order_type="FAK",
                market_slug=trade.event_slug,
                outcome=trade.side,
            )
            trade.exit_order_id = order.order_id
            trade.mode = "pending_exit"
            trade.updated_at = now
            trade.last_reason = f"exit_posted:{reason}:market_fak"
            return trade
        except Exception as exc:
            trade.mode = "pending_exit"
            trade.exit_order_id = None
            trade.updated_at = now
            trade.last_reason = f"close_failed_residual_position:{round(qty, 6)}:{type(exc).__name__}"
            return trade
    post_price = min(0.99, max(0.01, float(exit_price) if exit_price > 0 else 0.01))
    order_type = "FAK" if aggressive_exit or qty < float(min_limit_exit_qty) else "GTC"
    req = BrokerOrderRequest(
        token_id=trade.token_id or "",
        side="SELL",
        price=post_price,
        size=float(qty),
        order_type=order_type,
        market_slug=trade.event_slug,
        outcome=trade.side,
        client_order_key=f"current_almost_resolved:exit:{reason}:{int(now)}:{trade.side}",
    )
    try:
        order = broker.place_limit_order(req)
        trade.exit_order_id = order.order_id
        trade.mode = "pending_exit"
        trade.updated_at = now
        trade.last_reason = f"exit_posted:{reason}:{req.order_type.lower()}"
    except Exception as exc:
        trade.mode = "pending_exit"
        trade.exit_order_id = None
        trade.updated_at = now
        trade.last_reason = f"close_failed_residual_position:{round(qty, 6)}:{type(exc).__name__}"
    return trade


def _resolve_pending_entry_fast(
    broker,
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    entry_timeout_secs: float,
    signal: Optional[dict],
    snap: Optional[dict],
    qty: int,
    cfg: CurrentAlmostResolvedConfigV1,
    min_profit_ticks_after_entry: int,
    reprice_ticks: int,
    reprice_timeout_secs: float,
    state_path: Path,
    log_path: Path,
    session_id: str,
) -> LiveCurrentAlmostResolvedTradeState:
    if trade.mode != "pending_entry":
        return trade
    remaining = max(0.0, float(entry_timeout_secs) - max(0.0, time.time() - trade.created_at))
    if remaining > 0:
        time.sleep(remaining)
    now = time.time()
    cancel_resp = None
    if trade.entry_order_id:
        try:
            cancel_resp = broker.cancel_order(trade.entry_order_id)
        except Exception as exc:
            cancel_resp = {"error": f"{type(exc).__name__}: {exc}"}
    trade = _sync_entry_order(broker, trade)
    trade.updated_at = now
    if trade.entry_qty_filled > 0:
        trade.mode = "open_position"
        trade.updated_at = now
        trade.last_reason = "entry_fill_detected"
        _save_state(state_path, trade)
        _append_jsonl(
            log_path,
            {
                "type": "fill",
                "ts": now,
                "session_id": session_id,
                "cancel_remainder": cancel_resp,
                "trade": _trade_summary(trade),
            },
        )
        return trade
    if trade.entry_order_id and trade.entry_reprice_count <= 0:
        trade, reprice_result = _maybe_reprice_pending_entry(
            broker,
            trade,
            signal=signal or {},
            snap=snap,
            qty=qty,
            now=now,
            cfg=cfg,
            min_profit_ticks_after_entry=min_profit_ticks_after_entry,
            reprice_ticks=reprice_ticks,
        )
        if reprice_result is not None:
            _save_state(state_path, trade)
            _append_jsonl(
                log_path,
                {
                    "type": "entry_reprice",
                    "ts": now,
                    "session_id": session_id,
                    "result": reprice_result,
                    "signal": signal,
                    "trade": _trade_summary(trade),
                },
            )
            if reprice_result.get("status") == "repriced":
                if reprice_timeout_secs > 0:
                    time.sleep(reprice_timeout_secs)
                now = time.time()
                try:
                    cancel_resp = broker.cancel_order(trade.entry_order_id)
                except Exception as exc:
                    cancel_resp = {"error": f"{type(exc).__name__}: {exc}"}
                trade = _sync_entry_order(broker, trade)
                trade.updated_at = now
                if trade.entry_qty_filled > 0:
                    trade.mode = "open_position"
                    trade.last_reason = "entry_fill_detected_after_reprice"
                    _save_state(state_path, trade)
                    _append_jsonl(
                        log_path,
                        {
                            "type": "fill",
                            "ts": now,
                            "session_id": session_id,
                            "cancel_remainder": cancel_resp,
                            "trade": _trade_summary(trade),
                        },
                    )
                    return trade
    _append_jsonl(
        log_path,
        {
            "type": "entry_cancel",
            "ts": now,
            "session_id": session_id,
            "reason": "entry_timeout_fast_path",
            "response": cancel_resp,
            "trade": _trade_summary(trade),
        },
    )
    trade = LiveCurrentAlmostResolvedTradeState()
    _clear_state(state_path)
    return trade


def _force_risk_cleanup(broker, trade: LiveCurrentAlmostResolvedTradeState, log_path: Path, now: float, reason: str, min_limit_exit_qty: float) -> None:
    try:
        _append_jsonl(log_path, {"type": "panic", "ts": now, "reason": reason, "trade": _trade_summary(trade)})
        _cancel_if_live(broker, trade.entry_order_id)
        _cancel_if_live(broker, trade.exit_order_id)
        token_balance_qty = _token_balance_qty(broker, trade.token_id)
        panic_qty = token_balance_qty if token_balance_qty > 0 else trade.remaining_position_qty
        if not _is_flat_qty(panic_qty) and trade.token_id and trade.side:
            active_book = _fetch_active_book(trade)
            panic_bid = _best_bid(active_book or {})
            _post_exit_order(
                broker,
                trade,
                exit_price=panic_bid if panic_bid > 0 else 0.01,
                now=now,
                reason="panic",
                min_limit_exit_qty=min_limit_exit_qty,
            )
            _append_jsonl(log_path, {"type": "panic_exit_attempted", "ts": now, "panic_bid": panic_bid, "trade": _trade_summary(trade)})
    except Exception as exc:
        _append_jsonl(log_path, {"type": "panic_error", "ts": now, "reason": reason, "error": f"{type(exc).__name__}: {exc}"})


def _archive_residual_dust(
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    token_balance_qty: float,
    now: float,
    session_id: str,
    reason: str,
) -> None:
    _append_jsonl(
        _residuals_path(),
        {
            "type": "archived_residual_dust",
            "ts": now,
            "session_id": session_id,
            "reason": reason,
            "token_balance_qty": token_balance_qty,
            "trade": _trade_summary(trade),
        },
    )


def _shutdown_reconcile(
    broker,
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    min_limit_exit_qty: float,
    dust_archive_qty: float,
    state_path: Path,
    log_path: Path,
    session_id: str,
    now: float,
) -> LiveCurrentAlmostResolvedTradeState:
    try:
        reconciled = _restore_trade_from_broker(broker, trade)
    except Exception:
        reconciled = trade

    try:
        open_orders = [o.as_dict() for o in broker.get_open_orders()[:50]]
    except Exception:
        open_orders = []

    if reconciled.mode == "pending_entry":
        cancel_resp = _cancel_if_live(broker, reconciled.entry_order_id)
        reconciled = _restore_trade_from_broker(broker, reconciled)
        _append_jsonl(
            log_path,
            {
                "type": "shutdown_entry_cancel",
                "ts": now,
                "session_id": session_id,
                "cancel": cancel_resp,
                "trade": _trade_summary(reconciled),
            },
        )

    token_balance = _token_balance_qty(broker, reconciled.token_id)
    if (
        reconciled.mode != "idle"
        and not _is_flat_qty(token_balance)
        and token_balance <= float(dust_archive_qty)
        and not open_orders
    ):
        _archive_residual_dust(
            reconciled,
            token_balance_qty=token_balance,
            now=now,
            session_id=session_id,
            reason="shutdown_dust_archive",
        )
        _append_jsonl(
            log_path,
            {
                "type": "shutdown_dust_archived",
                "ts": now,
                "session_id": session_id,
                "token_balance_qty": token_balance,
                "trade": _trade_summary(reconciled),
            },
        )
        _clear_state(state_path)
        return LiveCurrentAlmostResolvedTradeState()

    if reconciled.mode != "idle" and not _is_flat_qty(token_balance) and reconciled.token_id and reconciled.side:
        active_book = _fetch_active_book(reconciled)
        shutdown_bid = _best_bid(active_book or {})
        reconciled = _post_exit_order(
            broker,
            reconciled,
            exit_price=shutdown_bid if shutdown_bid > 0 else 0.01,
            now=now,
            reason="shutdown_flatten",
            min_limit_exit_qty=min_limit_exit_qty,
        )
        _append_jsonl(
            log_path,
            {
                "type": "shutdown_exit_posted",
                "ts": now,
                "session_id": session_id,
                "bid": shutdown_bid,
                "token_balance_qty": token_balance,
                "trade": _trade_summary(reconciled),
            },
        )
        try:
            open_orders = [o.as_dict() for o in broker.get_open_orders()[:50]]
        except Exception:
            open_orders = []
        token_balance = _token_balance_qty(broker, reconciled.token_id)

    if reconciled.mode == "idle" or (not open_orders and _is_flat_qty(token_balance)):
        _append_jsonl(
            log_path,
            {
                "type": "shutdown_flat",
                "ts": now,
                "session_id": session_id,
                "open_orders": open_orders,
                "token_balance_qty": token_balance,
                "trade": _trade_summary(reconciled),
            },
        )
        _clear_state(state_path)
        return LiveCurrentAlmostResolvedTradeState()

    _save_state(state_path, reconciled)
    _append_jsonl(
        log_path,
        {
            "type": "shutdown_non_idle",
            "ts": now,
            "session_id": session_id,
            "open_orders": open_orders,
            "token_balance_qty": token_balance,
            "trade": _trade_summary(reconciled),
        },
    )
    return reconciled


def monitor_live_current_almost_resolved_real_v1(duration_seconds: Optional[int] = None, log_dir: Optional[str] = None) -> None:
    load_dotenv()
    guarded_cfg = load_live_guarded_config()
    broker_status = load_broker_env()
    signal_cfg = CurrentAlmostResolvedConfigV1()
    scalp_cfg = CurrentScalpConfigV1()

    print("[BROKER_ENV]", broker_status.as_dict())
    print("[LIVE_GUARDED_CONFIG]", guarded_cfg.as_dict())
    print("[CURRENT_ALMOST_RESOLVED_CONFIG]", signal_cfg.as_dict())
    print("[CURRENT_SCALP_CONTEXT_CONFIG]", scalp_cfg.as_dict())

    if not guarded_cfg.enabled:
        print("[GUARD] Set POLY_GUARDED_ENABLED=true")
        return
    if guarded_cfg.shadow_only:
        print("[GUARD] Set POLY_GUARDED_SHADOW_ONLY=false")
        return
    if not guarded_cfg.real_posts_enabled:
        print("[GUARD] Set POLY_GUARDED_REAL_POSTS_ENABLED=true")
        return
    if not _env_bool("POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED", False):
        print("[GUARD] Set POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED=true to arm real current almost resolved")
        return
    if not broker_status.ready_for_real_smoke:
        print("[GUARD] Broker env missing required credentials")
        return

    qty = _env_int("POLY_CURRENT_ALMOST_RESOLVED_QTY", 6)
    entry_timeout_secs = _env_float("POLY_CURRENT_ALMOST_RESOLVED_ENTRY_TIMEOUT_SECS", 2.0)
    entry_reprice_ticks = _env_int("POLY_CURRENT_ALMOST_RESOLVED_ENTRY_REPRICE_TICKS", 1)
    entry_reprice_timeout_secs = _env_float("POLY_CURRENT_ALMOST_RESOLVED_ENTRY_REPRICE_TIMEOUT_SECS", 1.0)
    exit_repost_secs = _env_float("POLY_CURRENT_ALMOST_RESOLVED_EXIT_REPOST_SECS", 1.0)
    flatten_deadline_secs = _env_int("POLY_CURRENT_ALMOST_RESOLVED_FLATTEN_DEADLINE_SECS", 2)
    min_limit_exit_qty = _env_float("POLY_CURRENT_ALMOST_RESOLVED_MIN_LIMIT_EXIT_QTY", 5.0)
    dust_archive_qty = _env_float("POLY_CURRENT_ALMOST_RESOLVED_DUST_ARCHIVE_QTY", 0.01)
    entry_premium_ticks = _env_int("POLY_CURRENT_ALMOST_RESOLVED_ENTRY_PREMIUM_TICKS", 1)
    min_profit_ticks_after_entry = _env_int("POLY_CURRENT_ALMOST_RESOLVED_MIN_PROFIT_TICKS_AFTER_ENTRY", 1)
    reference_cache_ttl_secs = _env_float("POLY_CURRENT_ALMOST_RESOLVED_REFERENCE_CACHE_TTL_SECS", 1.0)
    poll_secs = max(0.25, _env_float("POLY_CURRENT_ALMOST_RESOLVED_POLL_SECS", 0.5))
    run_for = int(duration_seconds or _env_int("POLY_CURRENT_ALMOST_RESOLVED_RUN_SECONDS", 1800))
    session_dir = Path(log_dir) if log_dir else _build_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "current_almost_resolved_real.jsonl"
    exception_path = session_dir / "exception.log"
    state_path = _state_path()
    session_id = session_dir.name

    print(
        "[CURRENT_ALMOST_RESOLVED_REAL_PARAMS]",
        {
            "qty": qty,
            "entry_timeout_secs": entry_timeout_secs,
            "entry_reprice_ticks": entry_reprice_ticks,
            "entry_reprice_timeout_secs": entry_reprice_timeout_secs,
            "exit_repost_secs": exit_repost_secs,
            "flatten_deadline_secs": flatten_deadline_secs,
            "min_limit_exit_qty": min_limit_exit_qty,
            "dust_archive_qty": dust_archive_qty,
            "entry_premium_ticks": entry_premium_ticks,
            "min_profit_ticks_after_entry": min_profit_ticks_after_entry,
            "reference_cache_ttl_secs": reference_cache_ttl_secs,
            "poll_secs": poll_secs,
            "run_for": run_for,
            "log_path": str(log_path),
            "state_path": str(state_path),
        },
    )

    broker = PolymarketBrokerV3.from_env()
    health = broker.healthcheck()
    print("[BROKER_HEALTH]", health.as_dict())
    if not health.ok:
        print("[GUARD] Broker healthcheck failed")
        return

    startup_orders = broker.get_open_orders()[:50]
    print("[BROKER_OPEN_ORDERS_STARTUP]", [o.as_dict() for o in startup_orders])
    restored_trade = _load_state(state_path) or LiveCurrentAlmostResolvedTradeState()
    _append_jsonl(
        log_path,
        {
            "type": "startup",
            "ts": time.time(),
            "session_id": session_id,
            "startup_orders": [o.as_dict() for o in startup_orders],
            "restored_trade": _trade_summary(restored_trade),
        },
    )

    if restored_trade.mode != "idle":
        restored_trade = _restore_trade_from_broker(broker, restored_trade)
        print("[RESTORED_CURRENT_ALMOST_RESOLVED_TRADE]", asdict(restored_trade))
        restored_balance = _token_balance_qty(broker, restored_trade.token_id)
        if restored_trade.mode == "idle":
            _clear_state(state_path)
        elif restored_balance <= float(dust_archive_qty):
            _archive_residual_dust(
                restored_trade,
                token_balance_qty=restored_balance,
                now=time.time(),
                session_id=session_id,
                reason="restore_dust_archive",
            )
            _append_jsonl(
                log_path,
                {
                    "type": "restore_dust_archived",
                    "ts": time.time(),
                    "session_id": session_id,
                    "token_balance_qty": restored_balance,
                    "trade": _trade_summary(restored_trade),
                },
            )
            _clear_state(state_path)
            restored_trade = LiveCurrentAlmostResolvedTradeState()
        else:
            allowed_ids = {x for x in (restored_trade.entry_order_id, restored_trade.exit_order_id) if x}
            startup_ids = {o.order_id for o in startup_orders}
            if startup_ids - allowed_ids:
                print("[GUARD] Refusing to start with open orders not owned by restored current almost resolved state.")
                return
            _save_state(state_path, restored_trade)
    elif startup_orders:
        print("[GUARD] Refusing to start with open orders while no current almost resolved state is restored.")
        return

    current_scalp = CurrentScalpResearchV1(cfg=scalp_cfg)
    trade = restored_trade
    current_slot_cache = CurrentSlotCache()
    started_at = time.time()

    while time.time() - started_at < run_for:
        now = time.time()
        try:
            if trade.mode == "pending_entry":
                current_item, current_snap, current_exec_reason = _fetch_current_item_and_snap(current_slot_cache)
                current_secs = int(current_item.get("seconds_to_end")) if current_item and current_item.get("seconds_to_end") is not None else None
                reference = _cached_external_reference(current_slot_cache, ttl_secs=reference_cache_ttl_secs) if current_item else {}
                current_scalp_signal = (
                    current_scalp.evaluate(
                        snap=current_snap,
                        secs_to_end=current_secs,
                        event_start_time=current_slot_cache.event_start_time,
                        now_ts=now,
                        reference_price=reference.get("reference_price"),
                        source_divergence_bps=reference.get("source_divergence_bps"),
                        opening_reference_price=current_slot_cache.opening_reference_price,
                    )
                    if current_item and current_snap
                    else {"setup": "no_edge", "allow": False, "reason": "missing_current"}
                )
                signal = (
                    evaluate_current_almost_resolved_v1(
                        snap=current_snap,
                        secs_to_end=current_secs,
                        reference_signal=current_scalp_signal,
                        cfg=signal_cfg,
                    )
                    if current_item and current_snap
                    else {"setup": "almost_resolved", "allow": False, "reason": "missing_current"}
                )
                if current_item:
                    signal["event_slug"] = current_item.get("slug")
                trade = _resolve_pending_entry_fast(
                    broker,
                    trade,
                    entry_timeout_secs=entry_timeout_secs,
                    signal=signal,
                    snap=current_snap,
                    qty=qty,
                    cfg=signal_cfg,
                    min_profit_ticks_after_entry=min_profit_ticks_after_entry,
                    reprice_ticks=entry_reprice_ticks,
                    reprice_timeout_secs=entry_reprice_timeout_secs,
                    state_path=state_path,
                    log_path=log_path,
                    session_id=session_id,
                )
                if trade.mode == "idle":
                    time.sleep(poll_secs)
                    continue

            current_item, current_snap, current_exec_reason = _fetch_current_item_and_snap(current_slot_cache)
            current_secs = int(current_item.get("seconds_to_end")) if current_item and current_item.get("seconds_to_end") is not None else None
            reference = _cached_external_reference(current_slot_cache, ttl_secs=reference_cache_ttl_secs) if current_item else {}
            current_scalp_signal = (
                current_scalp.evaluate(
                    snap=current_snap,
                    secs_to_end=current_secs,
                    event_start_time=current_slot_cache.event_start_time,
                    now_ts=now,
                    reference_price=reference.get("reference_price"),
                    source_divergence_bps=reference.get("source_divergence_bps"),
                    opening_reference_price=current_slot_cache.opening_reference_price,
                )
                if current_item and current_snap
                else {"setup": "no_edge", "allow": False, "reason": "missing_current"}
            )
            signal = (
                evaluate_current_almost_resolved_v1(
                    snap=current_snap,
                    secs_to_end=current_secs,
                    reference_signal=current_scalp_signal,
                    cfg=signal_cfg,
                )
                if current_item and current_snap
                else {"setup": "almost_resolved", "allow": False, "reason": "missing_current"}
            )
            if current_item:
                signal["event_slug"] = current_item.get("slug")

            active_book = _fetch_active_book(trade) if trade.mode in ("pending_entry", "open_position", "pending_exit", "exit_pending_confirm") else None
            active_bid = _best_bid(active_book or {})
            if trade.side and current_snap:
                exec_bid = _bid_for_side(current_snap, trade.side)
                if exec_bid > 0:
                    active_bid = max(active_bid, exec_bid)

            snapshot = {
                "type": "snapshot",
                "ts": now,
                "session_id": session_id,
                "current_slug": current_item.get("slug") if current_item else None,
                "current_secs": current_secs,
                "current_exec_reason": current_exec_reason,
                "reference": reference,
                "current_scalp_context": current_scalp_signal,
                "signal": signal,
                "trade": _trade_summary(trade),
                "active_bid": active_bid,
            }
            _append_jsonl(log_path, snapshot)
            print(
                f"[CURRENT_ALMOST_RESOLVED_REAL] current_secs={current_secs} allow={signal.get('allow')} "
                f"side={signal.get('side')} mode={trade.mode} qty={trade.entry_qty_filled}/{trade.remaining_position_qty}"
            )

            if trade.mode == "idle" and current_item and signal.get("allow"):
                side = str(signal.get("side") or "")
                tick_size = _tick_size_from_snap(current_snap, side)
                entry_price = _safe_float(signal.get("entry_price"), 0.0)
                execution_plan = _entry_execution_plan(
                    signal=signal,
                    tick_size=tick_size,
                    default_premium_ticks=entry_premium_ticks,
                    default_timeout_secs=entry_timeout_secs,
                    min_profit_ticks_after_entry=min_profit_ticks_after_entry,
                )
                planned_entry_price = _safe_float(execution_plan.get("entry_price"), entry_price)
                if not _has_sufficient_collateral_for_entry(broker, entry_price=planned_entry_price, qty=qty):
                    _append_jsonl(
                        log_path,
                        {
                            "type": "entry_blocked_insufficient_collateral",
                            "ts": now,
                            "session_id": session_id,
                            "required": round(planned_entry_price * qty + 0.25, 6),
                            "available": _collateral_balance_usd(broker),
                            "signal": signal,
                            "execution_plan": execution_plan,
                        },
                    )
                    time.sleep(poll_secs)
                    continue
                trade = _post_entry_order(
                    broker,
                    signal=signal,
                    snap=current_snap,
                    qty=qty,
                    tick_size=tick_size,
                    now=now,
                    cfg=signal_cfg,
                    execution_plan=execution_plan,
                )
                _save_state(state_path, trade)
                _append_jsonl(
                    log_path,
                    {
                        "type": "enter",
                        "ts": now,
                        "session_id": session_id,
                        "signal": signal,
                        "execution_plan": execution_plan,
                        "requested_entry_price": planned_entry_price,
                        "trade": _trade_summary(trade),
                    },
                )
                trade = _resolve_pending_entry_fast(
                    broker,
                    trade,
                    entry_timeout_secs=_safe_float(execution_plan.get("timeout_secs"), entry_timeout_secs),
                    signal=signal,
                    snap=current_snap,
                    qty=qty,
                    cfg=signal_cfg,
                    min_profit_ticks_after_entry=min_profit_ticks_after_entry,
                    reprice_ticks=entry_reprice_ticks,
                    reprice_timeout_secs=entry_reprice_timeout_secs,
                    state_path=state_path,
                    log_path=log_path,
                    session_id=session_id,
                )
                if trade.mode == "idle":
                    time.sleep(poll_secs)
                    continue
                _save_state(state_path, trade)
                time.sleep(poll_secs)
                continue

            if trade.mode in ("pending_entry", "open_position", "pending_exit", "exit_pending_confirm"):
                trade = _sync_entry_order(broker, trade)
                trade.updated_at = now
                _save_state(state_path, trade)

            if trade.mode == "pending_entry":
                if current_secs is not None and current_secs <= signal_cfg.min_secs_to_end:
                    resp = _cancel_if_live(broker, trade.entry_order_id)
                    _append_jsonl(
                        log_path,
                        {
                            "type": "entry_cancel",
                            "ts": now,
                            "session_id": session_id,
                            "reason": "entry_too_late_for_fill_window",
                            "response": resp,
                            "trade": _trade_summary(trade),
                        },
                    )
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)

            if trade.mode == "open_position" and trade.side:
                tick_size = _tick_size_from_snap(current_snap, trade.side)
                if _should_hold_to_resolution(signal, bid_now=active_bid, secs_to_end=current_secs, cfg=signal_cfg, side=trade.side):
                    trade.hold_to_resolution = True
                reason = _exit_reason(
                    trade,
                    bid_now=active_bid,
                    tick_size=tick_size,
                    now=now,
                    secs_to_end=current_secs,
                    signal=signal,
                    cfg=signal_cfg,
                    flatten_deadline_secs=flatten_deadline_secs,
                )
                if reason:
                    trade = _post_exit_order(
                        broker,
                        trade,
                        exit_price=active_bid,
                        now=now,
                        reason=reason,
                        min_limit_exit_qty=min_limit_exit_qty,
                    )
                    _save_state(state_path, trade)
                    _append_jsonl(log_path, {"type": "exit_posted", "ts": now, "session_id": session_id, "reason": reason, "trade": _trade_summary(trade)})

            if trade.mode == "pending_exit":
                token_balance_qty = _token_balance_qty(broker, trade.token_id)
                exit_order = _get_order_status(broker, trade.exit_order_id)
                if exit_order is None:
                    if _is_flat_qty(token_balance_qty):
                        _append_jsonl(log_path, {"type": "flat", "ts": now, "session_id": session_id, "exit_order": None, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                        trade = LiveCurrentAlmostResolvedTradeState()
                        _clear_state(state_path)
                    elif token_balance_qty <= float(dust_archive_qty):
                        _archive_residual_dust(
                            trade,
                            token_balance_qty=token_balance_qty,
                            now=now,
                            session_id=session_id,
                            reason="pending_exit_dust_archive",
                        )
                        _append_jsonl(
                            log_path,
                            {
                                "type": "dust_archived",
                                "ts": now,
                                "session_id": session_id,
                                "token_balance_qty": token_balance_qty,
                                "trade": _trade_summary(trade),
                            },
                        )
                        trade = LiveCurrentAlmostResolvedTradeState()
                        _clear_state(state_path)
                    elif now - trade.updated_at >= exit_repost_secs:
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=active_bid if active_bid > 0 else 0.01,
                            now=now,
                            reason="retry_residual",
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "exit_repost", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                else:
                    trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
                    status = str(getattr(exit_order, "status", "") or "").lower()
                    if _is_flat_qty(token_balance_qty) or trade.remaining_position_qty <= 0:
                        _append_jsonl(log_path, {"type": "flat", "ts": now, "session_id": session_id, "exit_order": exit_order.as_dict(), "exit_status": status, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                        trade = LiveCurrentAlmostResolvedTradeState()
                        _clear_state(state_path)
                    elif _is_match_status(status) and token_balance_qty > 0:
                        trade.mode = "exit_pending_confirm"
                        trade.last_reason = f"exit_match_pending_confirm:{round(token_balance_qty, 6)}"
                        trade.confirm_started_at = now
                        trade.confirm_polls = 1
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "exit_match_pending_confirm", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                    elif now - trade.updated_at >= exit_repost_secs:
                        cancel_resp = _cancel_if_live(broker, trade.exit_order_id)
                        trade.exit_order_id = None
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=active_bid if active_bid > 0 else 0.01,
                            now=now,
                            reason="repost",
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "exit_repost", "ts": now, "session_id": session_id, "cancel": cancel_resp, "trade": _trade_summary(trade)})

            if trade.mode == "exit_pending_confirm":
                token_balance_qty = _token_balance_qty(broker, trade.token_id)
                if _is_flat_qty(token_balance_qty):
                    _append_jsonl(log_path, {"type": "flat", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)
                elif now - trade.confirm_started_at >= 2.0 or trade.confirm_polls >= 2:
                    trade.mode = "pending_exit"
                    trade.exit_order_id = None
                    trade.updated_at = now - exit_repost_secs
                    trade.last_reason = f"residual_position_after_exit:{round(token_balance_qty, 6)}"
                    _save_state(state_path, trade)
                    _append_jsonl(log_path, {"type": "residual_position_after_exit", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                else:
                    trade.confirm_polls += 1
                    _save_state(state_path, trade)

            time.sleep(poll_secs)

        except Exception as exc:
            trace = traceback.format_exc()
            exception_path.write_text(trace, encoding="utf-8")
            _append_jsonl(
                log_path,
                {
                    "type": "exception",
                    "ts": now,
                    "session_id": session_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": trace,
                    "trade": _trade_summary(trade),
                },
            )
            _force_risk_cleanup(broker, trade, log_path, now, f"{type(exc).__name__}: {exc}", min_limit_exit_qty)
            _save_state(state_path, trade)
            raise

    _shutdown_reconcile(
        broker,
        trade,
        min_limit_exit_qty=min_limit_exit_qty,
        dust_archive_qty=dust_archive_qty,
        state_path=state_path,
        log_path=log_path,
        session_id=session_id,
        now=time.time(),
    )


if __name__ == "__main__":
    monitor_live_current_almost_resolved_real_v1()
