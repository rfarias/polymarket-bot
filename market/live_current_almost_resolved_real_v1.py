from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import asdict, dataclass
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
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _fetch_slot_state, _slot_snapshot
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


def _exception_message(exc: Exception) -> str:
    for attr in ("error_msg", "msg"):
        value = getattr(exc, attr, None)
        if value:
            return str(value)
    text = str(exc)
    return text if text else repr(exc)


def _is_fak_no_match_exception(exc: Exception) -> bool:
    text = _exception_message(exc).lower()
    return "no orders found to match with fak order" in text


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_log_dir() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"current_almost_resolved_real_{ts}"


def _state_path() -> Path:
    return Path("logs") / "current_almost_resolved_real_state.json"


def _counter_reversal_state_path() -> Path:
    return Path("logs") / "counter_reversal_real_state.json"


def _state_file_active(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return str(payload.get("mode") or "idle") != "idle"


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
    mode: str = "idle"  # idle | pending_entry | open_position | pending_exit | exit_pending_confirm | awaiting_redeem
    event_slug: Optional[str] = None
    side: Optional[str] = None
    token_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    entry_order_style: str = "unknown"  # patient_limit | passive_limit | aggressive_limit | direct_limit | unknown
    entry_order_type: str = "GTC"
    entry_initial_size_matched: float = 0.0
    setup_variant: Optional[str] = None
    entry_price: Optional[float] = None
    entry_qty_requested: float = 0.0
    entry_qty_filled: float = 0.0
    exit_qty_filled: float = 0.0
    exit_price_posted: Optional[float] = None
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    best_bid: Optional[float] = None
    hold_to_resolution: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    confirm_started_at: float = 0.0
    confirm_polls: int = 0
    resolution_detected_at: float = 0.0
    redeem_attempted_at: float = 0.0
    redeem_required: bool = False
    last_reason: Optional[str] = None

    @property
    def remaining_position_qty(self) -> float:
        return round(max(0.0, float(self.entry_qty_filled) - float(self.exit_qty_filled)), 6)


def _trade_summary(trade: LiveCurrentAlmostResolvedTradeState) -> dict:
    return asdict(trade)


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


def _ask_for_side(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_ask" if side == "UP" else "down_ask"), 0.0)


def _side_winning(side: str, signed_distance_from_open_bps: float) -> bool:
    return (side == "UP" and signed_distance_from_open_bps > 0) or (side == "DOWN" and signed_distance_from_open_bps < 0)


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


def _clamp_limit_price(price: float, *, tick_size: float) -> float:
    tick = max(0.001, _safe_float(tick_size, 0.001))
    bounded = min(max(tick, _safe_float(price, tick)), 1.0 - tick)
    return round(bounded, 6)


def _side_book(snap: dict, side: str) -> dict:
    return (snap.get("up") if side == "UP" else snap.get("down")) or {}


def _bid_levels_for_exit(snap: dict, side: str) -> list:
    """Exit liquidity for a position in `side` comes from the counter side's asks.
    In Polymarket binary CLOBs, selling DOWN is matched by UP asks (and vice versa).
    Converts counter-ask prices to equivalent same-side bid prices (1 - counter_ask)."""
    counter = "UP" if side == "DOWN" else "DOWN"
    levels = (_side_book(snap, counter).get("top_asks") or [])
    result = []
    for level in levels:
        if isinstance(level, dict):
            p = _safe_float(level.get("price"), 0.0)
            s = _safe_float(level.get("size"), 0.0)
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            p = _safe_float(level[0], 0.0)
            s = _safe_float(level[1], 0.0)
        else:
            continue
        if p <= 0.0 or p >= 1.0 or s <= 0.0:
            continue
        result.append({"price": round(1.0 - p, 6), "size": s})
    result.sort(key=lambda lvl: lvl["price"], reverse=True)
    return result


def _exit_liquidity_risk(
    snap: dict,
    executable: Optional[dict],
    *,
    side: str,
    entry_price: float,
    stop_price: float,
    qty: float,
    tick_size: float,
) -> dict:
    side = side if side in ("UP", "DOWN") else "UP"
    entry_price = _safe_float(entry_price, 0.0)
    stop_price = _safe_float(stop_price, 0.0)
    qty = max(0.0, _safe_float(qty, 0.0))
    tick_size = max(0.001, _safe_float(tick_size, 0.01))
    best_bid = _bid_for_side(executable, side)
    levels = _bid_levels_for_exit(snap, side)

    qty_at_or_above_stop = 0.0
    qty_at_best_three = 0.0
    remaining = qty
    notional = 0.0
    worst_price_for_qty = 0.0
    observed_bid_depth = 0.0

    for idx, level in enumerate(levels):
        price = _safe_float(level.get("price"), 0.0)
        size = _safe_float(level.get("size"), 0.0)
        if price <= 0 or size <= 0:
            continue
        observed_bid_depth += size
        if price >= stop_price:
            qty_at_or_above_stop += size
        if idx < 3:
            qty_at_best_three += size
        if remaining > 0:
            take = min(remaining, size)
            notional += take * price
            remaining = round(remaining - take, 6)
            worst_price_for_qty = price

    if qty <= 0:
        vwap_exit_for_qty = best_bid
        enough_depth_for_qty = True
    elif remaining <= 0:
        vwap_exit_for_qty = round(notional / qty, 6)
        enough_depth_for_qty = True
    else:
        vwap_exit_for_qty = round(notional / max(0.000001, qty - remaining), 6) if notional > 0 else 0.0
        enough_depth_for_qty = False

    theoretical_stop_loss_ticks = (
        round(max(0.0, entry_price - stop_price) / tick_size, 4) if entry_price > 0 and stop_price > 0 else None
    )
    best_bid_loss_ticks = (
        round(max(0.0, entry_price - best_bid) / tick_size, 4) if entry_price > 0 and best_bid > 0 else None
    )
    pessimistic_exit_loss_ticks = (
        round(max(0.0, entry_price - vwap_exit_for_qty) / tick_size, 4)
        if entry_price > 0 and vwap_exit_for_qty > 0
        else None
    )
    worst_level_loss_ticks = (
        round(max(0.0, entry_price - worst_price_for_qty) / tick_size, 4)
        if entry_price > 0 and worst_price_for_qty > 0
        else None
    )

    return {
        "side": side,
        "entry_price": round(entry_price, 6) if entry_price > 0 else None,
        "stop_price": round(stop_price, 6) if stop_price > 0 else None,
        "qty": round(qty, 6),
        "best_bid": round(best_bid, 6) if best_bid > 0 else None,
        "bid_levels_seen": len(levels),
        "observed_bid_depth": round(observed_bid_depth, 6),
        "qty_at_or_above_stop": round(qty_at_or_above_stop, 6),
        "qty_at_best_three": round(qty_at_best_three, 6),
        "vwap_exit_for_qty": vwap_exit_for_qty if vwap_exit_for_qty > 0 else None,
        "worst_price_for_qty": round(worst_price_for_qty, 6) if worst_price_for_qty > 0 else None,
        "enough_depth_for_qty": bool(enough_depth_for_qty),
        "theoretical_stop_loss_ticks": theoretical_stop_loss_ticks,
        "best_bid_loss_ticks": best_bid_loss_ticks,
        "pessimistic_exit_loss_ticks": pessimistic_exit_loss_ticks,
        "worst_level_loss_ticks": worst_level_loss_ticks,
        "exit_depth_covers_stop": bool(qty <= 0 or qty_at_or_above_stop >= qty),
    }


def _should_await_platform_redeem(
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    current_secs: Optional[int],
    current_slug: Optional[str],
    hold_winner_to_resolution: bool,
) -> bool:
    if not hold_winner_to_resolution:
        return False
    if not trade.token_id or not trade.side:
        return False
    event_rolled = bool(current_slug and trade.event_slug and current_slug != trade.event_slug)
    settle_window = current_secs is not None and current_secs <= 1
    return event_rolled or settle_window


def _passive_entry_price_for_signal(signal: dict, *, bid_now: float, tick_size: float) -> float:
    signal_entry = _safe_float(signal.get("entry_price"), 0.0)
    tick = max(0.001, _safe_float(tick_size, 0.001))
    anchor = _safe_float(bid_now, 0.0) if _safe_float(bid_now, 0.0) > 0 else signal_entry
    return round(max(tick, anchor - tick), 6)


def _runaway_chase_params(
    signal: dict,
    trade: "LiveCurrentAlmostResolvedTradeState",
    *,
    current_secs: Optional[int],
    max_chase_price: float = 0.98,
    max_chase_distance: float = 0.04,
    min_secs: int = 25,
) -> Optional[dict]:
    """
    Returns chase parameters if the market ran in the correct direction while a
    passive entry was pending (signal now invalid because price moved away from
    the limit). Returns None if conditions aren't met.

    Conditions:
    - current buy price > original entry price + 1 tick (market moved right)
    - chase via ask price <= max_chase_price (at least 1 cent headroom to $1)
    - chase distance <= max_chase_distance (don't overpay for stale momentum)
    - enough time remaining (>= min_secs)
    - no missing midpoint context
    """
    side = str(trade.side or "")
    if side not in ("UP", "DOWN"):
        return None
    original_entry = _safe_float(trade.entry_price, 0.0)
    if original_entry <= 0:
        return None

    buy_key = "up_buy" if side == "UP" else "down_buy"
    ask_key = "up_sell" if side == "UP" else "down_sell"
    current_buy = _safe_float(signal.get(buy_key), 0.0)
    current_ask = _safe_float(signal.get(ask_key), 0.0) or current_buy + 0.01

    chase_distance = current_buy - original_entry
    if chase_distance < 0.01:
        return None
    if chase_distance > max_chase_distance:
        return None
    if current_ask <= 0 or current_ask > max_chase_price:
        return None
    if current_secs is None or current_secs < min_secs:
        return None
    if bool(signal.get("missing_market_midpoint_context")):
        return None

    return {"ask": round(current_ask, 6), "buy": round(current_buy, 6), "chase_distance": round(chase_distance, 4)}


def _safe_to_chase_aggressive_entry(
    signal: dict,
    *,
    ask_now: float,
    current_secs: Optional[int],
    max_price: float,
) -> bool:
    if ask_now <= 0 or ask_now > max_price:
        return False
    if current_secs is None or current_secs > 35:
        return False
    if str(signal.get("setup_variant") or "") != "passive_extreme_liquidity_capture":
        return False
    side = str(signal.get("side") or "").lower()
    if bool(signal.get("missing_market_midpoint_context")):
        return False
    if side and bool(signal.get(f"{side}_counter_alert")):
        return False
    return bool(signal.get("resolved_pullback_safe_distance_ok")) or bool(signal.get("passive_capture_safe_distance_ok"))


def _is_transient_service_not_ready_exception(exc: Exception) -> bool:
    message = _exception_message(exc).lower()
    return "service not ready" in message or "status_code=425" in message or "status code=425" in message


def _has_sufficient_collateral_for_entry(broker, *, entry_price: float, qty: float, buffer_usd: float = 0.25) -> bool:
    required = round(float(entry_price) * float(qty) + float(buffer_usd), 6)
    return _collateral_balance_usd(broker) >= required


def _sync_entry_order(broker, trade: LiveCurrentAlmostResolvedTradeState) -> LiveCurrentAlmostResolvedTradeState:
    order = _get_order_status(broker, trade.entry_order_id)
    if order is not None:
        trade.entry_qty_filled = max(trade.entry_qty_filled, _safe_float(getattr(order, "size_matched", None), 0.0))
    token_balance = _token_balance_qty(broker, trade.token_id)
    if token_balance > 0:
        trade.entry_qty_filled = max(trade.entry_qty_filled, token_balance + float(trade.exit_qty_filled))
    return trade


def _restore_trade_from_broker(broker, trade: LiveCurrentAlmostResolvedTradeState) -> LiveCurrentAlmostResolvedTradeState:
    if trade.mode in ("idle", "awaiting_redeem"):
        return trade
    trade = _sync_entry_order(broker, trade)
    exit_order = _get_order_status(broker, trade.exit_order_id)
    if exit_order is not None:
        trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
        status = str(getattr(exit_order, "status", "") or "").lower()
        if _is_flat_qty(_token_balance_qty(broker, trade.token_id)) or trade.remaining_position_qty <= 0:
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
    hold_winner_to_resolution: bool,
) -> Optional[str]:
    if bid_now <= 0 or trade.entry_price is None:
        return None
    side = trade.side or "UP"
    trade.best_bid = max(_safe_float(trade.best_bid), bid_now)
    pnl_ticks_now = (bid_now - float(trade.entry_price)) / tick_size if tick_size > 0 else 0.0
    buffer_bps = _safe_float(signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"), 0.0)
    open_distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    market_range_15s = _safe_float(signal.get("market_range_15s"), 0.0)
    market_range_30s = _safe_float(signal.get("market_range_30s"), 0.0)
    edge_vs_counter = _safe_float(signal.get("up_edge_vs_counter" if side == "UP" else "down_edge_vs_counter"), 0.0)
    adverse_spot_bps = _safe_float(signal.get("up_adverse_spot_bps" if side == "UP" else "down_adverse_spot_bps"), 0.0)

    if not hold_winner_to_resolution and secs_to_end is not None and secs_to_end <= flatten_deadline_secs:
        return "deadline_flatten"
    if not hold_winner_to_resolution and bid_now >= _safe_float(trade.target_price):
        return "target"
    setup_variant = str(trade.setup_variant or signal.get("setup_variant") or "")
    if (
        setup_variant == "controlled_late_entry"
        and pnl_ticks_now > 0
        and (
            market_range_15s >= cfg.controlled_late_max_market_range_15s
            or adverse_spot_bps >= cfg.controlled_late_max_adverse_spot_15s_bps
            or buffer_bps <= cfg.paper_structural_stop_buffer_bps
            or open_distance_bps <= cfg.min_price_to_beat_distance_bps
        )
    ):
        return "controlled_late_profit_take"
    if (
        setup_variant == "resolved_pullback_limit"
        and (
            market_range_15s >= cfg.near_end_max_market_range_15s
            or market_range_30s >= cfg.paper_profit_take_on_market_range_30s
            or adverse_spot_bps >= cfg.controlled_late_max_adverse_spot_15s_bps
        )
    ):
        return "resolved_pullback_exit"
    if bid_now <= _safe_float(trade.stop_price):
        return "stop"
    if setup_variant == "extreme_99_limit":
        trade.hold_to_resolution = True
        return None
    if (
        pnl_ticks_now >= cfg.paper_profit_take_min_ticks
        and (
            (not hold_winner_to_resolution and secs_to_end is not None and secs_to_end <= cfg.paper_profit_take_late_secs)
            or buffer_bps <= cfg.paper_profit_take_on_reversal_buffer_bps
            or market_range_30s >= cfg.paper_profit_take_on_market_range_30s
            or adverse_spot_bps >= open_distance_bps * cfg.max_reversal_share_of_open_distance
        )
    ):
        return "profit_protect"
    if (
        not hold_winner_to_resolution
        and pnl_ticks_now > 0
        and not trade.hold_to_resolution
        and secs_to_end is not None
        and secs_to_end <= cfg.paper_hold_to_resolution_secs
    ):
        return "late_profit_take"
    # In a binary CLOB, up_ask + down_ask ≈ 1.0. If both sides show prices > 0.50
    # (sum > 1.10), the snapshot is stale/corrupt. Skip structural_stop in that case
    # to avoid exiting profitable positions based on bad data.
    _snap_up_ask = _safe_float(signal.get("up_buy"), 0.0)
    _snap_down_ask = _safe_float(signal.get("down_buy"), 0.0)
    _snap_data_ok = not (_snap_up_ask > 0 and _snap_down_ask > 0 and _snap_up_ask + _snap_down_ask > 1.10)
    if _snap_data_ok and (
        buffer_bps <= cfg.paper_structural_stop_buffer_bps
        or market_range_30s >= cfg.paper_structural_stop_market_range_30s
        or edge_vs_counter <= cfg.paper_structural_stop_edge_vs_counter
        or (signal.get("side") not in (None, side) and signal.get("allow"))
    ):
        return "structural_stop"
    if not hold_winner_to_resolution and not trade.hold_to_resolution and now - trade.created_at >= cfg.max_hold_secs:
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
    entry_price_override: Optional[float] = None,
    order_style: str = "direct_limit",
    aggressive_entry_fak: bool = True,
) -> LiveCurrentAlmostResolvedTradeState:
    side = str(signal.get("side") or "")
    entry_price = _safe_float(entry_price_override, _safe_float(signal.get("entry_price"), 0.0))
    order_type = "FAK" if order_style == "aggressive_limit" and aggressive_entry_fak else "GTC"
    trade = LiveCurrentAlmostResolvedTradeState(
        mode="pending_entry",
        event_slug=str(signal.get("event_slug") or ""),
        side=side,
        token_id=_token_id_for_side(snap, side),
        entry_order_style=order_style,
        entry_order_type=order_type,
        setup_variant=str(signal.get("setup_variant") or "standard"),
        entry_price=entry_price,
        entry_qty_requested=float(qty),
        target_price=round(min(0.99, _safe_float(signal.get("exit_price"), cfg.target_exit_price)), 6),
        stop_price=round(
            max(
                0.01,
                _safe_float(signal.get("stop_price"), entry_price - cfg.stop_ticks * tick_size),
            ),
            6,
        ),
        created_at=now,
        updated_at=now,
        last_reason="entry_posted",
    )
    if not trade.token_id:
        raise RuntimeError(f"Missing token_id for side={side}")
    if qty < 5:
        raise RuntimeError("Current almost resolved real requires qty >= 5.")
    if not _has_sufficient_collateral_for_entry(broker, entry_price=entry_price, qty=qty):
        raise RuntimeError(
            f"Insufficient collateral for entry: required={round(entry_price * qty, 6)} available={_collateral_balance_usd(broker)}"
        )
    req = BrokerOrderRequest(
        token_id=trade.token_id,
        side="BUY",
        price=entry_price,
        size=float(qty),
        order_type=order_type,
        market_slug=trade.event_slug,
        outcome=side,
        client_order_key=f"current_almost_resolved:entry:{order_style}:{int(now)}:{side}",
    )
    order = broker.place_limit_order(req)
    trade.entry_order_id = order.order_id
    trade.entry_initial_size_matched = _safe_float(getattr(order, "size_matched", None), 0.0)
    trade.entry_qty_filled = max(trade.entry_qty_filled, trade.entry_initial_size_matched)
    trade.last_reason = f"entry_posted:{order_style}:{order_type.lower()}:matched={round(trade.entry_initial_size_matched, 6)}"
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
    if _is_flat_qty(qty):
        trade.mode = "idle"
        trade.last_reason = "flat"
        trade.updated_at = now
        return trade
    try:
        broker.update_balance_allowance(asset_type="CONDITIONAL", token_id=trade.token_id)
    except Exception:
        pass
    if qty < float(min_limit_exit_qty) and hasattr(broker, "place_market_order"):
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
            trade.last_reason = f"close_failed_residual_position:{round(qty, 6)}:{type(exc).__name__}:{_exception_message(exc)}"
            return trade
    active_book = _fetch_active_book(trade)
    tick_size = _safe_float((active_book or {}).get("tick_size"), 0.001)
    post_price = _clamp_limit_price(float(exit_price) if exit_price > 0 else tick_size, tick_size=tick_size)
    # Urgent exits (stop-type) use FAK to guarantee immediate fill rather than leaving
    # a GTC resting in the book while the market continues to move against the position.
    # Non-urgent exits (profit targets) use GTC so the order can rest at the desired price.
    _urgent = any(k in reason for k in ("stop", "deadline_flatten", "resolved_pullback", "controlled_late_profit", "fill_signal_invalid"))
    req = BrokerOrderRequest(
        token_id=trade.token_id or "",
        side="SELL",
        price=post_price,
        size=float(qty),
        order_type="FAK" if _urgent else ("GTC" if qty >= float(min_limit_exit_qty) else "FAK"),
        market_slug=trade.event_slug,
        outcome=trade.side,
        client_order_key=f"current_almost_resolved:exit:{reason}:{int(now)}:{trade.side}",
    )
    try:
        order = broker.place_limit_order(req)
        trade.exit_order_id = order.order_id
        trade.exit_price_posted = post_price
        trade.mode = "pending_exit"
        trade.updated_at = now
        trade.last_reason = f"exit_posted:{reason}:{req.order_type.lower()}"
    except Exception as exc:
        trade.mode = "pending_exit"
        trade.exit_order_id = None
        trade.updated_at = now
        trade.last_reason = f"close_failed_residual_position:{round(qty, 6)}:{type(exc).__name__}:{_exception_message(exc)}"
    return trade


def _mark_awaiting_redeem(
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    now: float,
    reason: str,
) -> LiveCurrentAlmostResolvedTradeState:
    trade.mode = "awaiting_redeem"
    trade.entry_order_id = None
    trade.exit_order_id = None
    trade.updated_at = now
    trade.resolution_detected_at = now
    trade.redeem_required = True
    trade.last_reason = reason
    return trade


def _attempt_redeem_if_available(broker, trade: LiveCurrentAlmostResolvedTradeState) -> Optional[dict]:
    for method_name in ("redeem_positions", "redeem_position", "claim", "claim_rewards"):
        method = getattr(broker, method_name, None)
        if callable(method):
            try:
                return {"method": method_name, "response": method(trade)}
            except TypeError:
                try:
                    return {"method": method_name, "response": method()}
                except Exception as exc:
                    return {"method": method_name, "error": f"{type(exc).__name__}: {exc}"}
            except Exception as exc:
                return {"method": method_name, "error": f"{type(exc).__name__}: {exc}"}
    client = getattr(broker, "client", None)
    for method_name in ("redeem_positions", "redeem_position", "claim", "claim_rewards"):
        method = getattr(client, method_name, None)
        if callable(method):
            try:
                return {"method": f"client.{method_name}", "response": method()}
            except Exception as exc:
                return {"method": f"client.{method_name}", "error": f"{type(exc).__name__}: {exc}"}
    return None


def _force_risk_cleanup(broker, trade: LiveCurrentAlmostResolvedTradeState, log_path: Path, now: float, reason: str, min_limit_exit_qty: float) -> None:
    try:
        _append_jsonl(log_path, {"type": "panic", "ts": now, "reason": reason, "trade": _trade_summary(trade)})
        if trade.mode == "awaiting_redeem":
            _append_jsonl(log_path, {"type": "panic_skip_awaiting_redeem", "ts": now, "reason": reason, "trade": _trade_summary(trade)})
            return
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


def _shutdown_reconcile(
    broker,
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    min_limit_exit_qty: float,
    state_path: Path,
    log_path: Path,
    session_id: str,
    now: float,
) -> LiveCurrentAlmostResolvedTradeState:
    if trade.mode == "awaiting_redeem":
        _save_state(state_path, trade)
        _append_jsonl(
            log_path,
            {
                "type": "shutdown_awaiting_redeem",
                "ts": now,
                "session_id": session_id,
                "trade": _trade_summary(trade),
            },
        )
        return trade

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
    passive_capture_only = _env_bool("POLY_CURRENT_ALMOST_RESOLVED_PASSIVE_CAPTURE_ONLY", True)
    hybrid_entry_enabled = _env_bool("POLY_CURRENT_ALMOST_RESOLVED_HYBRID_ENTRY", True)
    hybrid_aggressive_after_secs = _env_float("POLY_CURRENT_ALMOST_RESOLVED_HYBRID_AGGRESSIVE_AFTER_SECS", 2.0)
    hybrid_aggressive_max_price = _env_float("POLY_CURRENT_ALMOST_RESOLVED_HYBRID_AGGRESSIVE_MAX_PRICE", 0.99)
    aggressive_entry_fak = _env_bool("POLY_CURRENT_ALMOST_RESOLVED_AGGRESSIVE_ENTRY_FAK", True)
    runaway_chase_max_price = _env_float("POLY_CURRENT_ALMOST_RESOLVED_RUNAWAY_CHASE_MAX_PRICE", 0.98)
    runaway_chase_max_distance = _env_float("POLY_CURRENT_ALMOST_RESOLVED_RUNAWAY_CHASE_MAX_DISTANCE", 0.04)
    runaway_chase_min_secs = _env_int("POLY_CURRENT_ALMOST_RESOLVED_RUNAWAY_CHASE_MIN_SECS", 25)
    hold_winner_to_resolution = _env_bool("POLY_CURRENT_ALMOST_RESOLVED_HOLD_WINNER_TO_RESOLUTION", True)
    resolution_settle_secs = _env_int("POLY_CURRENT_ALMOST_RESOLVED_RESOLUTION_SETTLE_SECS", 1)
    auto_redeem_enabled = _env_bool("POLY_CURRENT_ALMOST_RESOLVED_AUTO_REDEEM_ENABLED", False)
    dust_archive_qty = _env_float("POLY_CURRENT_ALMOST_RESOLVED_DUST_ARCHIVE_QTY", 0.01)
    exit_repost_secs = _env_float("POLY_CURRENT_ALMOST_RESOLVED_EXIT_REPOST_SECS", 1.0)
    flatten_deadline_secs = _env_int("POLY_CURRENT_ALMOST_RESOLVED_FLATTEN_DEADLINE_SECS", 2)
    min_limit_exit_qty = _env_float("POLY_CURRENT_ALMOST_RESOLVED_MIN_LIMIT_EXIT_QTY", 5.0)
    poll_secs = max(0.25, _env_float("POLY_CURRENT_ALMOST_RESOLVED_POLL_SECS", 0.5))
    run_for = int(duration_seconds or _env_int("POLY_CURRENT_ALMOST_RESOLVED_RUN_SECONDS", 1800))
    session_dir = Path(log_dir) if log_dir else _build_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "current_almost_resolved_real.jsonl"
    exception_path = session_dir / "exception.log"
    state_path = _state_path()
    session_id = session_dir.name
    blocked_entry_events: dict[str, str] = {}

    print(
        "[CURRENT_ALMOST_RESOLVED_REAL_PARAMS]",
        {
            "qty": qty,
            "entry_timeout_secs": entry_timeout_secs,
            "passive_capture_only": passive_capture_only,
            "hybrid_entry_enabled": hybrid_entry_enabled,
            "hybrid_aggressive_after_secs": hybrid_aggressive_after_secs,
            "hybrid_aggressive_max_price": hybrid_aggressive_max_price,
            "aggressive_entry_fak": aggressive_entry_fak,
            "hold_winner_to_resolution": hold_winner_to_resolution,
            "resolution_settle_secs": resolution_settle_secs,
            "auto_redeem_enabled": auto_redeem_enabled,
            "dust_archive_qty": dust_archive_qty,
            "exit_repost_secs": exit_repost_secs,
            "flatten_deadline_secs": flatten_deadline_secs,
            "min_limit_exit_qty": min_limit_exit_qty,
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
        if restored_trade.mode == "idle":
            _clear_state(state_path)
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
    current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}
    last_leader_price_by_key: dict[str, float] = {}
    leader_velocity_history: dict[str, list[tuple[float, float]]] = {}
    started_at = time.time()

    while time.time() - started_at < run_for:
        now = time.time()
        try:
            slot_bundle = _build_slot_bundle()
            current_item = slot_bundle["queue"].get("current")
            current_secs = int(current_item.get("seconds_to_end")) if current_item and current_item.get("seconds_to_end") is not None else None
            slot_state = _fetch_slot_state(slot_bundle)
            current_snap = _slot_snapshot(slot_state, "current")
            current_exec, current_exec_reason = _compute_executable_metrics(current_snap)

            if current_item and current_item.get("slug") != current_open_reference.get("slug"):
                raw_event = fetch_event_by_slug(str(current_item.get("slug") or ""))
                market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
                event_start_time = market.get("eventStartTime") or raw_event.get("startTime") if raw_event else None
                open_ref = fetch_binance_open_price_for_event_start_v1(event_start_time) if event_start_time else {"open_price": None}
                current_open_reference = {
                    "slug": current_item.get("slug"),
                    "price": open_ref.get("open_price"),
                    "event_start_time": event_start_time,
                }

            reference = fetch_external_btc_reference_v1() if current_item else {}
            current_scalp_signal = (
                current_scalp.evaluate(
                    snap=current_snap,
                    secs_to_end=current_secs,
                    event_start_time=current_open_reference.get("event_start_time"),
                    now_ts=now,
                    reference_price=reference.get("reference_price"),
                    source_divergence_bps=reference.get("source_divergence_bps"),
                    opening_reference_price=current_open_reference.get("price"),
                )
                if current_item
                else {"setup": "no_edge", "allow": False, "reason": "missing_current"}
            )
            signal = (
                evaluate_current_almost_resolved_v1(
                    snap=current_snap,
                    secs_to_end=current_secs,
                    reference_signal=current_scalp_signal,
                    cfg=signal_cfg,
                )
                if current_item
                else {"setup": "almost_resolved", "allow": False, "reason": "missing_current"}
            )
            if current_item:
                signal["event_slug"] = current_item.get("slug")
                signal["signed_distance_from_open_bps"] = current_scalp_signal.get("distance_from_open_bps")
                signal["guardian_hold_winner_to_resolution"] = bool(hold_winner_to_resolution)

            # Update rolling 60s leader velocity history for both sides
            _event_slug_for_vel = str(current_item.get("slug") or "") if current_item else ""
            if _event_slug_for_vel:
                for _vel_side, _vel_key in (("UP", "up_buy"), ("DOWN", "down_buy")):
                    _vel_price = _safe_float(signal.get(_vel_key), 0.0)
                    if _vel_price > 0:
                        _hist_key = f"{_event_slug_for_vel}:{_vel_side}"
                        _hist = leader_velocity_history.setdefault(_hist_key, [])
                        _hist.append((now, _vel_price))
                        leader_velocity_history[_hist_key] = [(t, p) for t, p in _hist if now - t <= 60.0]

            active_book = _fetch_active_book(trade) if trade.mode in ("pending_entry", "open_position", "pending_exit", "exit_pending_confirm") else None
            active_bid = _best_bid(active_book or {})
            if trade.side and current_exec:
                exec_bid = _bid_for_side(current_exec, trade.side)
                if exec_bid > 0:
                    active_bid = max(active_bid, exec_bid)
            counter_reversal_active = _state_file_active(_counter_reversal_state_path())

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
                "counter_reversal_active": counter_reversal_active,
            }
            _append_jsonl(log_path, snapshot)
            print(
                f"[CURRENT_ALMOST_RESOLVED_REAL] current_secs={current_secs} allow={signal.get('allow')} "
                f"side={signal.get('side')} mode={trade.mode} qty={trade.entry_qty_filled}/{trade.remaining_position_qty}"
            )

            if trade.mode == "idle" and current_item and signal.get("allow") and not counter_reversal_active:
                event_slug = str(signal.get("event_slug") or current_item.get("slug") or "")
                if passive_capture_only and str(signal.get("setup_variant") or "") != "passive_extreme_liquidity_capture":
                    _append_jsonl(
                        log_path,
                        {
                            "type": "entry_blocked",
                            "ts": now,
                            "session_id": session_id,
                            "reason": "passive_capture_only",
                            "signal": signal,
                        },
                    )
                    time.sleep(poll_secs)
                    continue
                if event_slug and event_slug in blocked_entry_events:
                    _append_jsonl(
                        log_path,
                        {
                            "type": "entry_blocked",
                            "ts": now,
                            "session_id": session_id,
                            "reason": f"event_entry_cooldown:{blocked_entry_events[event_slug]}",
                            "signal": signal,
                        },
                    )
                    time.sleep(poll_secs)
                    continue
                side = str(signal.get("side") or "")
                tick_size = _tick_size_from_snap(current_snap, side)
                signal_entry_price = _safe_float(signal.get("entry_price"), 0.0)
                entry_price = signal_entry_price
                order_style = "direct_limit"
                if hybrid_entry_enabled:
                    current_bid = _bid_for_side(current_exec, side)
                    entry_price = _passive_entry_price_for_signal(
                        signal,
                        bid_now=current_bid,
                        tick_size=tick_size,
                    )
                    order_style = "passive_limit"

                # tick_up_confirmed: for passive entries, only proceed if leader_price
                # has ticked up since the last poll on this (slug, side) pair. Prevents
                # posting passive orders on stale signals that have already lost momentum.
                is_passive_signal = (
                    str(signal.get("setup_variant") or "") == "passive_extreme_liquidity_capture"
                    and str(signal.get("execution_style") or "") == "post_only"
                )
                leader_price = _safe_float(signal.get("leader_price"), 0.0)
                leader_key = f"{event_slug}:{side}"
                prev_leader_price = last_leader_price_by_key.get(leader_key)
                tick_up_confirmed = (
                    not is_passive_signal
                    or prev_leader_price is None
                    or leader_price > prev_leader_price
                )
                if side in ("UP", "DOWN") and leader_price > 0:
                    last_leader_price_by_key[leader_key] = leader_price
                if not tick_up_confirmed:
                    _append_jsonl(
                        log_path,
                        {
                            "type": "entry_blocked",
                            "ts": now,
                            "session_id": session_id,
                            "reason": "passive_tick_up_required",
                            "leader_price": leader_price,
                            "prev_leader_price": prev_leader_price,
                            "signal": signal,
                        },
                    )
                    time.sleep(poll_secs)
                    continue

                # Block entry if leader moved too fast (dual-window velocity filter).
                # Blocks if: vel_range_30s >= 0.04  OR  vel_range_60s >= 0.10
                # Old single rule (>= 0.06 on 60s) was too aggressive for spikes
                # that already faded — market_range_15s/30s is near 0 but 60s window
                # still "sees" the old move, blocking clean setups unnecessarily.
                _vel_hist = leader_velocity_history.get(leader_key, [])
                if len(_vel_hist) >= 2:
                    _vel_prices_60 = [p for _, p in _vel_hist]
                    _vel_prices_30 = [p for t, p in _vel_hist if now - t <= 30.0]
                    _vel_range_60 = max(_vel_prices_60) - min(_vel_prices_60)
                    _vel_range_30 = (max(_vel_prices_30) - min(_vel_prices_30)) if len(_vel_prices_30) >= 2 else 0.0
                    if _vel_range_60 >= 0.10 or _vel_range_30 >= 0.04:
                        _append_jsonl(
                            log_path,
                            {
                                "type": "entry_blocked",
                                "ts": now,
                                "session_id": session_id,
                                "reason": "leader_velocity_too_high",
                                "velocity_range": round(_vel_range_60, 4),
                                "velocity_range_30s": round(_vel_range_30, 4),
                                "velocity_window_secs": 60.0,
                                "threshold": 0.10,
                                "threshold_30s": 0.04,
                                "signal": signal,
                            },
                        )
                        time.sleep(poll_secs)
                        continue

                planned_exit_risk = _exit_liquidity_risk(
                    current_snap,
                    current_exec,
                    side=side,
                    entry_price=entry_price,
                    stop_price=round(max(0.01, _safe_float(signal.get("stop_price"), entry_price - signal_cfg.stop_ticks * tick_size)), 6),
                    qty=float(qty),
                    tick_size=tick_size,
                )

                try:
                    trade = _post_entry_order(
                        broker,
                        signal=signal,
                        snap=current_snap,
                        qty=qty,
                        tick_size=tick_size,
                        now=now,
                        cfg=signal_cfg,
                        entry_price_override=entry_price,
                        order_style=order_style,
                        aggressive_entry_fak=aggressive_entry_fak,
                    )
                except Exception as exc:
                    if _is_transient_service_not_ready_exception(exc):
                        _append_jsonl(
                            log_path,
                            {
                                "type": "entry_transient_error",
                                "ts": now,
                                "session_id": session_id,
                                "stage": "initial_entry",
                                "error": f"{type(exc).__name__}: {_exception_message(exc)}",
                                "signal": signal,
                                "entry_order_style": order_style,
                                "posted_entry_price": entry_price,
                            },
                        )
                        time.sleep(poll_secs)
                        continue
                    if order_style == "aggressive_limit" and aggressive_entry_fak and _is_fak_no_match_exception(exc):
                        if event_slug:
                            blocked_entry_events[event_slug] = "initial_fak_no_match"
                        _append_jsonl(
                            log_path,
                            {
                                "type": "entry_fak_no_match",
                                "ts": now,
                                "session_id": session_id,
                                "stage": "initial_entry",
                                "error": f"{type(exc).__name__}: {_exception_message(exc)}",
                                "signal": signal,
                                "entry_order_style": order_style,
                                "posted_entry_price": entry_price,
                            },
                        )
                        time.sleep(poll_secs)
                        continue
                    raise
                _save_state(state_path, trade)
                _append_jsonl(
                    log_path,
                    {
                        "type": "enter",
                        "ts": now,
                        "session_id": session_id,
                        "signal": signal,
                        "hybrid_entry_enabled": hybrid_entry_enabled,
                        "entry_order_style": order_style,
                        "signal_entry_price": signal_entry_price,
                        "posted_entry_price": entry_price,
                        "planned_exit_risk": planned_exit_risk,
                        "trade": _trade_summary(trade),
                    },
                )
                time.sleep(poll_secs)
                continue
            if trade.mode == "idle" and current_item and signal.get("allow") and counter_reversal_active:
                _append_jsonl(
                    log_path,
                    {
                        "type": "entry_blocked",
                        "ts": now,
                        "session_id": session_id,
                        "reason": "counter_reversal_active",
                        "signal": signal,
                    },
                )

            if trade.mode in ("pending_entry", "open_position", "pending_exit", "exit_pending_confirm"):
                trade = _sync_entry_order(broker, trade)
                trade.updated_at = now
                _save_state(state_path, trade)

            if trade.mode == "pending_entry":
                if trade.entry_qty_filled > 0:
                    resp = _cancel_if_live(broker, trade.entry_order_id)
                    trade.mode = "open_position"
                    trade.updated_at = now
                    trade.last_reason = "entry_fill_detected"
                    _save_state(state_path, trade)
                    # Detect fills where the signal is already invalid or bid already breached stop.
                    # Log separately so these can be identified in analysis.
                    _fill_bid = active_bid
                    _fill_stop = _safe_float(trade.stop_price, 0.0)
                    _fill_signal_ok = bool(signal.get("allow")) and signal.get("side") == trade.side
                    _fill_at_stop = _fill_stop > 0 and _fill_bid > 0 and _fill_bid <= _fill_stop
                    _fill_event = "fill_on_invalid_signal" if (not _fill_signal_ok or _fill_at_stop) else "fill"
                    _append_jsonl(log_path, {"type": _fill_event, "ts": now, "session_id": session_id, "cancel_remainder": resp, "fill_bid": _fill_bid, "fill_signal_ok": _fill_signal_ok, "fill_at_stop": _fill_at_stop, "trade": _trade_summary(trade)})
                elif trade.entry_order_style in ("patient_limit", "passive_limit"):
                    current_slug = str(current_item.get("slug") or "") if current_item else ""
                    signal_still_valid = (
                        bool(current_item)
                        and not counter_reversal_active
                        and bool(signal.get("allow"))
                        and signal.get("side") == trade.side
                        and signal.get("event_slug") == trade.event_slug
                    )
                    event_rolled = bool(current_slug and trade.event_slug and current_slug != trade.event_slug)
                    last_second = current_secs is not None and current_secs <= resolution_settle_secs
                    if event_rolled or last_second or not signal_still_valid:
                        cancel_reason = (
                            "passive_event_rolled"
                            if event_rolled
                            else "passive_last_second"
                            if last_second
                            else "passive_signal_invalidated"
                        )

                        # Runaway aggressive chase: signal became invalid because market
                        # moved in the correct direction without revisiting our passive limit.
                        # Cancel the passive and replace with a FAK at the current ask.
                        _runaway = None
                        if (
                            cancel_reason == "passive_signal_invalidated"
                            and hybrid_entry_enabled
                            and trade.entry_qty_filled <= 0
                        ):
                            _runaway = _runaway_chase_params(
                                signal,
                                trade,
                                current_secs=current_secs,
                                max_chase_price=runaway_chase_max_price,
                                max_chase_distance=runaway_chase_max_distance,
                                min_secs=runaway_chase_min_secs,
                            )

                        if _runaway and trade.entry_qty_filled <= 0:
                            _runaway_cancel_resp = _cancel_if_live(broker, trade.entry_order_id)
                            # Re-check: did the passive fill during cancel?
                            _synced = _sync_entry_order(broker, trade)
                            if _synced.entry_qty_filled > 0:
                                trade = _synced
                                trade.mode = "open_position"
                                trade.updated_at = now
                                trade.last_reason = "runaway_passive_filled_before_chase"
                                _save_state(state_path, trade)
                                _append_jsonl(log_path, {"type": "fill", "ts": now, "session_id": session_id, "cancel_remainder": _runaway_cancel_resp, "fill_bid": active_bid, "fill_signal_ok": False, "fill_at_stop": False, "trade": _trade_summary(trade)})
                                time.sleep(poll_secs)
                                continue
                            _runaway_from = _trade_summary(trade)
                            _runaway_chase_price = _runaway["ask"]
                            _runaway_tick_size = _tick_size_from_snap(current_snap, trade.side or "UP")
                            try:
                                trade = _post_entry_order(
                                    broker,
                                    signal=signal,
                                    snap=current_snap,
                                    qty=qty,
                                    tick_size=_runaway_tick_size,
                                    now=now,
                                    cfg=signal_cfg,
                                    entry_price_override=_runaway_chase_price,
                                    order_style="aggressive_limit",
                                    aggressive_entry_fak=True,
                                )
                                _save_state(state_path, trade)
                                _append_jsonl(log_path, {
                                    "type": "entry_runaway_chase",
                                    "ts": now,
                                    "session_id": session_id,
                                    "cancel": _runaway_cancel_resp,
                                    "from_trade": _runaway_from,
                                    "chase_price": _runaway_chase_price,
                                    "chase_distance": _runaway["chase_distance"],
                                    "runaway": _runaway,
                                    "signal": signal,
                                    "trade": _trade_summary(trade),
                                })
                            except Exception as _runaway_exc:
                                _is_no_match = aggressive_entry_fak and _is_fak_no_match_exception(_runaway_exc)
                                if trade.event_slug:
                                    blocked_entry_events[str(trade.event_slug)] = "runaway_fak_no_match" if _is_no_match else "runaway_chase_error"
                                _append_jsonl(log_path, {
                                    "type": "entry_runaway_chase_failed",
                                    "ts": now,
                                    "session_id": session_id,
                                    "cancel": _runaway_cancel_resp,
                                    "from_trade": _runaway_from,
                                    "chase_price": _runaway_chase_price,
                                    "error": f"{type(_runaway_exc).__name__}: {_exception_message(_runaway_exc)}",
                                    "signal": signal,
                                })
                                trade = LiveCurrentAlmostResolvedTradeState()
                                _clear_state(state_path)
                            time.sleep(poll_secs)
                            continue

                        resp = _cancel_if_live(broker, trade.entry_order_id)
                        if trade.event_slug:
                            blocked_entry_events[trade.event_slug] = cancel_reason
                        _append_jsonl(
                            log_path,
                            {
                                "type": "entry_cancel",
                                "ts": now,
                                "session_id": session_id,
                                "reason": cancel_reason,
                                "response": resp,
                                "trade": _trade_summary(trade),
                                "signal": signal,
                            },
                        )
                        trade = LiveCurrentAlmostResolvedTradeState()
                        _clear_state(state_path)
                    elif hybrid_entry_enabled and now - trade.created_at >= hybrid_aggressive_after_secs:
                        aggressive_price = _ask_for_side(current_exec, trade.side or "")
                        if aggressive_price <= 0:
                            aggressive_price = _safe_float(signal.get("entry_price"), 0.0)
                        if _safe_to_chase_aggressive_entry(
                            signal,
                            ask_now=aggressive_price,
                            current_secs=current_secs,
                            max_price=hybrid_aggressive_max_price,
                        ):
                            previous_entry_order_id = trade.entry_order_id
                            cancel_resp = _cancel_if_live(broker, trade.entry_order_id)
                            previous_order = _get_order_status(broker, previous_entry_order_id)
                            previous_status = str(getattr(previous_order, "status", "") or "").lower() if previous_order is not None else ""
                            if previous_order is not None and previous_status in ("filled", "matched", "closed", "resolved"):
                                trade = _sync_entry_order(broker, trade)
                                _save_state(state_path, trade)
                                _append_jsonl(
                                    log_path,
                                    {
                                        "type": "entry_replace_blocked_previous_filled",
                                        "ts": now,
                                        "session_id": session_id,
                                        "cancel": cancel_resp,
                                        "previous_order": previous_order.as_dict(),
                                        "trade": _trade_summary(trade),
                                        "signal": signal,
                                    },
                                )
                                time.sleep(poll_secs)
                                continue
                            if previous_order is not None and previous_status not in ("canceled", "cancelled", "rejected"):
                                _append_jsonl(
                                    log_path,
                                    {
                                        "type": "entry_replace_blocked_cancel_unconfirmed",
                                        "ts": now,
                                        "session_id": session_id,
                                        "cancel": cancel_resp,
                                        "previous_order": previous_order.as_dict(),
                                        "aggressive_price": aggressive_price,
                                        "trade": _trade_summary(trade),
                                        "signal": signal,
                                    },
                                )
                                time.sleep(poll_secs)
                                continue
                            replaced_from = _trade_summary(trade)
                            tick_size = _tick_size_from_snap(current_snap, trade.side or "UP")
                            try:
                                trade = _post_entry_order(
                                    broker,
                                    signal=signal,
                                    snap=current_snap,
                                    qty=qty,
                                    tick_size=tick_size,
                                    now=now,
                                    cfg=signal_cfg,
                                    entry_price_override=round(aggressive_price, 6),
                                    order_style="aggressive_limit",
                                    aggressive_entry_fak=aggressive_entry_fak,
                                )
                            except Exception as exc:
                                if _is_transient_service_not_ready_exception(exc):
                                    trade = LiveCurrentAlmostResolvedTradeState()
                                    _clear_state(state_path)
                                    _append_jsonl(
                                        log_path,
                                        {
                                            "type": "entry_transient_error",
                                            "ts": now,
                                            "session_id": session_id,
                                            "stage": "replace_aggressive",
                                            "cancel": cancel_resp,
                                            "from_trade": replaced_from,
                                            "aggressive_price": aggressive_price,
                                            "error": f"{type(exc).__name__}: {_exception_message(exc)}",
                                            "signal": signal,
                                        },
                                    )
                                    time.sleep(poll_secs)
                                    continue
                                if aggressive_entry_fak and _is_fak_no_match_exception(exc):
                                    if replaced_from.get("event_slug"):
                                        blocked_entry_events[str(replaced_from.get("event_slug"))] = "replace_fak_no_match"
                                    trade = LiveCurrentAlmostResolvedTradeState()
                                    _clear_state(state_path)
                                    _append_jsonl(
                                        log_path,
                                        {
                                            "type": "entry_fak_no_match",
                                            "ts": now,
                                            "session_id": session_id,
                                            "stage": "replace_aggressive",
                                            "cancel": cancel_resp,
                                            "from_trade": replaced_from,
                                            "aggressive_price": aggressive_price,
                                            "error": f"{type(exc).__name__}: {_exception_message(exc)}",
                                            "signal": signal,
                                        },
                                    )
                                    time.sleep(poll_secs)
                                    continue
                                raise
                            _save_state(state_path, trade)
                            _append_jsonl(
                                log_path,
                                {
                                    "type": "entry_replace_aggressive_limit",
                                    "ts": now,
                                    "session_id": session_id,
                                    "cancel": cancel_resp,
                                    "from_trade": replaced_from,
                                    "aggressive_price": aggressive_price,
                                    "signal": signal,
                                    "trade": _trade_summary(trade),
                                },
                            )
                elif now - trade.created_at >= entry_timeout_secs or (
                    current_secs is not None
                    and current_secs
                    <= (
                        resolution_settle_secs
                        if trade.setup_variant == "extreme_99_limit"
                        else signal_cfg.min_secs_to_end
                    )
                ):
                    resp = _cancel_if_live(broker, trade.entry_order_id)
                    if trade.event_slug:
                        blocked_entry_events[trade.event_slug] = "entry_timeout_no_fill"
                    _append_jsonl(log_path, {"type": "entry_cancel", "ts": now, "session_id": session_id, "reason": "entry_timeout_no_fill", "response": resp, "trade": _trade_summary(trade)})
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)

            if trade.mode == "open_position" and trade.side:
                tick_size = _tick_size_from_snap(current_snap, trade.side)
                if _should_hold_to_resolution(signal, bid_now=active_bid, secs_to_end=current_secs, cfg=signal_cfg, side=trade.side):
                    trade.hold_to_resolution = True
                signed_distance = _safe_float(signal.get("signed_distance_from_open_bps"), 0.0)
                reached_resolution = (
                    hold_winner_to_resolution
                    and (
                        (current_secs is not None and current_secs <= resolution_settle_secs)
                        or (current_item and trade.event_slug and current_item.get("slug") != trade.event_slug)
                    )
                )
                platform_redeem_path = _should_await_platform_redeem(
                    trade,
                    current_secs=current_secs,
                    current_slug=str(current_item.get("slug") or "") if current_item else None,
                    hold_winner_to_resolution=hold_winner_to_resolution,
                )
                if reached_resolution or platform_redeem_path:
                    side_won = _side_winning(trade.side, signed_distance)
                    awaiting_reason = (
                        "platform_redeem_path_awaiting_final_balance"
                        if platform_redeem_path and not reached_resolution
                        else "resolution_win_awaiting_redeem" if side_won else "resolution_loss_or_unknown_awaiting_final_balance"
                    )
                    trade = _mark_awaiting_redeem(
                        trade,
                        now=now,
                        reason=awaiting_reason,
                    )
                    _save_state(state_path, trade)
                    _append_jsonl(
                        log_path,
                        {
                            "type": "awaiting_redeem",
                            "ts": now,
                            "session_id": session_id,
                            "side_won": side_won,
                            "signed_distance_from_open_bps": signed_distance,
                            "active_bid": active_bid,
                            "platform_redeem_path": platform_redeem_path,
                            "trade": _trade_summary(trade),
                        },
                    )
                else:
                    reason = _exit_reason(
                        trade,
                        bid_now=active_bid,
                        tick_size=tick_size,
                        now=now,
                        secs_to_end=current_secs,
                        signal=signal,
                        cfg=signal_cfg,
                        flatten_deadline_secs=flatten_deadline_secs,
                        hold_winner_to_resolution=hold_winner_to_resolution,
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

            if trade.mode == "awaiting_redeem":
                token_balance_qty = _token_balance_qty(broker, trade.token_id)
                collateral_balance = _collateral_balance_usd(broker)
                redeem_result = None
                if auto_redeem_enabled and now - _safe_float(trade.redeem_attempted_at, 0.0) >= 10.0:
                    redeem_result = _attempt_redeem_if_available(broker, trade)
                    trade.redeem_attempted_at = now
                    trade.updated_at = now
                    _save_state(state_path, trade)
                _append_jsonl(
                    log_path,
                    {
                        "type": "awaiting_redeem_status",
                        "ts": now,
                        "session_id": session_id,
                        "token_balance_qty": token_balance_qty,
                        "collateral_balance_usd": collateral_balance,
                        "auto_redeem_enabled": auto_redeem_enabled,
                        "redeem_result": redeem_result,
                        "trade": _trade_summary(trade),
                    },
                )
                if _is_flat_qty(token_balance_qty):
                    # Guard: the balance API can return 0 for ~10-15s after a fill
                    # while the blockchain state propagates. If we know the position
                    # was filled (entry_qty_filled > 0), wait for the API to catch up
                    # before declaring the trade closed — a premature reset here leaves
                    # real shares unmanaged.
                    known_filled = _safe_float(trade.entry_qty_filled, 0.0) > 0
                    secs_since_resolution = now - _safe_float(trade.resolution_detected_at, now)
                    if known_filled and secs_since_resolution < 15.0:
                        _append_jsonl(log_path, {"type": "awaiting_redeem_balance_lag", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "entry_qty_filled": trade.entry_qty_filled, "secs_since_resolution": round(secs_since_resolution, 2), "trade": _trade_summary(trade)})
                    else:
                        _append_jsonl(log_path, {"type": "redeem_flat", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "collateral_balance_usd": collateral_balance, "trade": _trade_summary(trade)})
                        trade = LiveCurrentAlmostResolvedTradeState()
                        _clear_state(state_path)
                elif 0 < token_balance_qty <= dust_archive_qty:
                    _append_jsonl(
                        log_path,
                        {
                            "type": "redeem_dust_archived",
                            "ts": now,
                            "session_id": session_id,
                            "token_balance_qty": token_balance_qty,
                            "dust_archive_qty": dust_archive_qty,
                            "trade": _trade_summary(trade),
                        },
                    )
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)

            if trade.mode == "pending_exit":
                token_balance_qty = _token_balance_qty(broker, trade.token_id)
                exit_order = _get_order_status(broker, trade.exit_order_id)
                if exit_order is None:
                    if _is_flat_qty(token_balance_qty):
                        _append_jsonl(log_path, {"type": "flat", "ts": now, "session_id": session_id, "exit_order": None, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                        trade = LiveCurrentAlmostResolvedTradeState()
                        _clear_state(state_path)
                    elif _should_await_platform_redeem(
                        trade,
                        current_secs=current_secs,
                        current_slug=str(current_item.get("slug") or "") if current_item else None,
                        hold_winner_to_resolution=hold_winner_to_resolution,
                    ):
                        trade = _mark_awaiting_redeem(
                            trade,
                            now=now,
                            reason=f"pending_exit_platform_redeem_path:{round(token_balance_qty, 6)}",
                        )
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "awaiting_redeem", "ts": now, "session_id": session_id, "token_balance_qty": token_balance_qty, "active_bid": active_bid, "trade": _trade_summary(trade)})
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
        state_path=state_path,
        log_path=log_path,
        session_id=session_id,
        now=time.time(),
    )


if __name__ == "__main__":
    monitor_live_current_almost_resolved_real_v1()
