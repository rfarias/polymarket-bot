from __future__ import annotations

import json
import os
import re
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from market.book_5m import fetch_books_for_tokens
from market.broker_env import load_broker_env
from market.broker_startup_guard_v1 import evaluate_startup_guard
from market.broker_types import BrokerOrderRequest
from market.current_scalp_signal_v1 import fetch_external_btc_reference_v1
from market.live_guarded_config import load_live_guarded_config
from market.next1_scalp_signal_v1 import Next1ScalpConfigV1, Next1ScalpResearchV1
from market.polymarket_broker_v3 import PolymarketBrokerV3
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _fetch_slot_state, _slot_snapshot
from market.setup1_broker_executor_v4 import Setup1BrokerExecutorV4


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


def _tick_size_from_snap(snap: dict, side: str) -> float:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(side_book.get("tick_size"), 0.01))


def _tick_size_from_book(book: dict, fallback: float = 0.01) -> float:
    return max(0.001, _safe_float(book.get("tick_size"), fallback))


def _bid_for_side(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _token_id_for_side(snap: dict, side: str) -> str:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return str(side_book.get("token_id") or "")


def _build_log_dir() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"next1_scalp_real_{ts}"


def _state_path() -> Path:
    return Path("logs") / "next1_scalp_real_state.json"


def _residuals_path() -> Path:
    return Path("logs") / "next1_scalp_real_residuals.jsonl"


def _parse_slug_epoch(slug: Optional[str]) -> Optional[int]:
    if not slug:
        return None
    match = re.search(r"(\d{10})$", str(slug))
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _seconds_to_end_from_slug(slug: Optional[str], now_ts: float) -> Optional[int]:
    start_epoch = _parse_slug_epoch(slug)
    if start_epoch is None:
        return None
    return max(0, int((start_epoch + 300) - now_ts))


def _best_bid(book: dict) -> float:
    bids = book.get("bids") or []
    if not bids:
        return 0.0
    return _safe_float((bids[0] or {}).get("price"), 0.0)


def _best_ask(book: dict) -> float:
    asks = book.get("asks") or []
    if not asks:
        return 0.0
    return _safe_float((asks[0] or {}).get("price"), 0.0)


def _save_state(path: Path, trade) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(trade), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state(path: Path):
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return LiveTradeState(**payload)
    except Exception:
        return None


def _clear_state(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


@dataclass
class LiveTradeState:
    trade_id: Optional[str] = None
    mode: str = "idle"  # idle | working_entry | entry_pending_confirm | open_position | pending_exit | exit_pending_confirm
    event_slug: Optional[str] = None
    side: Optional[str] = None
    token_id: Optional[str] = None
    setup: Optional[str] = None
    aggressive_entry_price: Optional[float] = None
    passive_entry_price: Optional[float] = None
    aggressive_order_id: Optional[str] = None
    passive_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    aggressive_qty_requested: float = 0.0
    passive_qty_requested: float = 0.0
    aggressive_qty_filled: float = 0.0
    passive_qty_filled: float = 0.0
    exit_qty_filled: float = 0.0
    entry_price_avg: Optional[float] = None
    best_bid: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_entry_reprice_at: float = 0.0
    last_reason: Optional[str] = None
    confirm_started_at: float = 0.0
    confirm_polls: int = 0

    @property
    def total_entry_qty(self) -> float:
        return round(float(self.aggressive_qty_filled) + float(self.passive_qty_filled), 6)

    @property
    def remaining_position_qty(self) -> float:
        return round(max(0.0, self.total_entry_qty - float(self.exit_qty_filled)), 6)


def _order_snapshot(order) -> Optional[dict]:
    if order is None:
        return None
    if hasattr(order, "as_dict"):
        try:
            return order.as_dict()
        except Exception:
            pass
    return {
        "order_id": getattr(order, "order_id", None),
        "status": getattr(order, "status", None),
        "price": getattr(order, "price", None),
        "original_size": getattr(order, "original_size", None),
        "size_matched": getattr(order, "size_matched", None),
        "remaining_size": getattr(order, "remaining_size", None),
        "token_id": getattr(order, "token_id", None),
        "side": getattr(order, "side", None),
    }


def _signal_summary(signal: dict) -> dict:
    return {
        "setup": signal.get("setup"),
        "allow": signal.get("allow"),
        "side": signal.get("side"),
        "reason": signal.get("reason"),
        "event_slug": signal.get("event_slug"),
        "entry_price": signal.get("entry_price"),
        "aggressive_entry_price": signal.get("aggressive_entry_price"),
        "exit_price": signal.get("exit_price"),
        "next1_secs": signal.get("next1_secs"),
        "current_secs": signal.get("current_secs"),
    }


def _trade_summary(trade: LiveTradeState) -> dict:
    return {
        "trade_id": trade.trade_id,
        "mode": trade.mode,
        "event_slug": trade.event_slug,
        "side": trade.side,
        "setup": trade.setup,
        "token_id": trade.token_id,
        "aggressive_order_id": trade.aggressive_order_id,
        "passive_order_id": trade.passive_order_id,
        "exit_order_id": trade.exit_order_id,
        "aggressive_qty_requested": trade.aggressive_qty_requested,
        "passive_qty_requested": trade.passive_qty_requested,
        "aggressive_qty_filled": trade.aggressive_qty_filled,
        "passive_qty_filled": trade.passive_qty_filled,
        "exit_qty_filled": trade.exit_qty_filled,
        "total_entry_qty": trade.total_entry_qty,
        "remaining_position_qty": trade.remaining_position_qty,
        "entry_price_avg": trade.entry_price_avg,
        "best_bid": trade.best_bid,
        "stop_price": trade.stop_price,
        "target_price": trade.target_price,
        "last_reason": trade.last_reason,
    }


def _append_trade_event(
    path: Path,
    *,
    event_type: str,
    session_id: str,
    now: float,
    trade: LiveTradeState,
    signal: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> None:
    row = {
        "type": event_type,
        "ts": now,
        "session_id": session_id,
        "trade": _trade_summary(trade),
    }
    if signal is not None:
        row["signal"] = _signal_summary(signal)
    if extra:
        row.update(extra)
    _append_jsonl(path, row)


def _rebuild_levels(trade: LiveTradeState, tick_size: float, cfg: Next1ScalpConfigV1) -> LiveTradeState:
    if trade.entry_price_avg is None:
        return trade
    trade.stop_price = round(max(0.01, float(trade.entry_price_avg) - cfg.stop_ticks * tick_size), 6)
    trade.target_price = round(min(0.99, float(trade.entry_price_avg) + cfg.target_ticks * tick_size), 6)
    return trade


def _average_entry_price(trade: LiveTradeState) -> Optional[float]:
    qty = trade.total_entry_qty
    if qty <= 0:
        return None
    gross = (
        _safe_float(trade.aggressive_entry_price) * float(trade.aggressive_qty_filled)
        + _safe_float(trade.passive_entry_price) * float(trade.passive_qty_filled)
    )
    return round(gross / qty, 6)


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


def _sync_entry_order(
    broker,
    trade: LiveTradeState,
    *,
    order_id: Optional[str],
    current_filled: float,
) -> float:
    order = _get_order_status(broker, order_id)
    if order is None:
        return current_filled
    return max(float(current_filled), _safe_float(getattr(order, "size_matched", None), 0.0))


def _fetch_active_book(trade: LiveTradeState) -> Optional[dict]:
    if not trade.token_id:
        return None
    raw_books = fetch_books_for_tokens([trade.token_id])
    if not raw_books:
        return None
    return raw_books[0] if raw_books else None


def _restore_trade_from_broker(broker, trade: LiveTradeState) -> LiveTradeState:
    if trade.mode == "idle":
        return trade
    trade.aggressive_qty_filled = _sync_entry_order(
        broker,
        trade,
        order_id=trade.aggressive_order_id,
        current_filled=trade.aggressive_qty_filled,
    )
    trade.passive_qty_filled = _sync_entry_order(
        broker,
        trade,
        order_id=trade.passive_order_id,
        current_filled=trade.passive_qty_filled,
    )
    if trade.total_entry_qty > 0 and trade.mode == "working_entry":
        trade.mode = "open_position"
    exit_order = _get_order_status(broker, trade.exit_order_id)
    if exit_order is not None:
        trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
        status = str(getattr(exit_order, "status", "") or "").lower()
        if trade.remaining_position_qty <= 0 or status in ("filled", "closed", "resolved"):
            return LiveTradeState()
        trade.mode = "pending_exit"
    trade.entry_price_avg = _average_entry_price(trade)
    trade = _reconcile_position_to_token_balance(broker, trade)
    active_book = _fetch_active_book(trade)
    tick_size = _tick_size_from_book(active_book or {}, 0.01)
    return _rebuild_levels(trade, tick_size=tick_size, cfg=Next1ScalpConfigV1())


def _post_entry_orders(
    broker,
    *,
    signal: dict,
    snap: dict,
    now: float,
    aggressive_qty: int,
    passive_qty: int,
) -> LiveTradeState:
    side = str(signal.get("side") or "")
    trade = LiveTradeState(
        trade_id=f"next1_scalp:{int(now)}:{side}",
        mode="working_entry",
        event_slug=str(signal.get("event_slug") or ""),
        side=side,
        token_id=_token_id_for_side(snap, side),
        setup=str(signal.get("setup") or ""),
        aggressive_entry_price=_safe_float(signal.get("aggressive_entry_price")),
        passive_entry_price=_safe_float(signal.get("entry_price")),
        aggressive_qty_requested=float(aggressive_qty),
        passive_qty_requested=float(passive_qty),
        created_at=now,
        updated_at=now,
        last_reason="entry_posted",
    )
    if not trade.token_id:
        raise RuntimeError(f"Missing token_id for side={side}")
    if not _has_sufficient_collateral_for_entry(broker, trade):
        trade.last_reason = "entry_blocked_insufficient_collateral"
        raise RuntimeError(
            f"Insufficient collateral for entry: required={_entry_notional_usd(trade)} available={_collateral_balance_usd(broker)}"
        )

    if aggressive_qty > 0 and trade.aggressive_entry_price and trade.aggressive_entry_price > 0:
        aggressive_req = BrokerOrderRequest(
            token_id=trade.token_id,
            side="BUY",
            price=float(trade.aggressive_entry_price),
            size=float(aggressive_qty),
            market_slug=trade.event_slug,
            outcome=side,
            client_order_key=f"next1_scalp:entry_aggr:{int(now)}:{side}",
        )
        aggressive_order = broker.place_limit_order(aggressive_req)
        trade.aggressive_order_id = aggressive_order.order_id

    if passive_qty > 0 and trade.passive_entry_price and trade.passive_entry_price > 0:
        try:
            passive_req = BrokerOrderRequest(
                token_id=trade.token_id,
                side="BUY",
                price=float(trade.passive_entry_price),
                size=float(passive_qty),
                market_slug=trade.event_slug,
                outcome=side,
                client_order_key=f"next1_scalp:entry_passive:{int(now)}:{side}",
            )
            passive_order = broker.place_limit_order(passive_req)
            trade.passive_order_id = passive_order.order_id
        except Exception:
            _cancel_if_live(broker, trade.aggressive_order_id)
            trade.aggressive_order_id = None
            trade.last_reason = "entry_aborted_passive_post_failed"
            raise

    if not trade.aggressive_order_id and not trade.passive_order_id:
        raise RuntimeError("No entry order was posted")

    return trade


def _entry_leg_remaining_qty(trade: LiveTradeState, leg: str) -> float:
    if leg == "aggressive":
        return round(max(0.0, float(trade.aggressive_qty_requested) - float(trade.aggressive_qty_filled)), 6)
    if leg == "passive":
        return round(max(0.0, float(trade.passive_qty_requested) - float(trade.passive_qty_filled)), 6)
    return 0.0


def _entry_signal_still_valid(trade: LiveTradeState, signal: dict, active_secs: Optional[int], cfg: Next1ScalpConfigV1) -> bool:
    if active_secs is not None and active_secs < cfg.min_secs_to_end:
        return False
    if not signal.get("allow"):
        return False
    if signal.get("side") != trade.side:
        return False
    if signal.get("event_slug") and signal.get("event_slug") != trade.event_slug:
        return False
    return True


def _entry_price_cap(signal: dict, cfg: Next1ScalpConfigV1) -> float:
    setup = str(signal.get("setup") or "")
    if "extreme" in setup:
        return float(cfg.extreme_next1_chase_price_cap)
    return float(cfg.next1_chase_price_cap)


def _adaptive_passive_entry_price(aggressive_price: float, ask_now: float, tick_size: float) -> float:
    spread = max(0.0, float(ask_now) - float(aggressive_price))
    if spread <= tick_size:
        return round(float(aggressive_price), 6)
    return max(0.01, round(float(aggressive_price) - tick_size, 6))


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


def _entry_notional_usd(trade: LiveTradeState) -> float:
    return round(
        _safe_float(trade.aggressive_entry_price) * float(trade.aggressive_qty_requested)
        + _safe_float(trade.passive_entry_price) * float(trade.passive_qty_requested),
        6,
    )


def _has_sufficient_collateral_for_entry(broker, trade: LiveTradeState, buffer_usd: float = 0.25) -> bool:
    required = _entry_notional_usd(trade) + float(buffer_usd)
    available = _collateral_balance_usd(broker)
    return available >= required


def _reconcile_position_to_token_balance(broker, trade: LiveTradeState) -> LiveTradeState:
    if trade.mode not in ("working_entry", "entry_pending_confirm", "open_position", "pending_exit", "exit_pending_confirm") or not trade.token_id:
        return trade
    token_balance = _token_balance_qty(broker, trade.token_id)
    gross_total = trade.total_entry_qty
    effective_total = round(float(trade.exit_qty_filled) + float(token_balance), 6)
    if effective_total <= 0:
        return trade
    if gross_total <= 0:
        trade.aggressive_qty_filled = effective_total
        trade.passive_qty_filled = 0.0
        trade.entry_price_avg = _average_entry_price(trade)
        return trade
    if effective_total <= gross_total:
        return trade
    scale = float(effective_total) / float(gross_total)
    trade.aggressive_qty_filled = round(float(trade.aggressive_qty_filled) * scale, 6)
    trade.passive_qty_filled = round(float(trade.passive_qty_filled) * scale, 6)
    trade.entry_price_avg = _average_entry_price(trade)
    return trade


def _book_is_extreme_or_invalid(bid: float, ask: float) -> bool:
    if bid <= 0 or ask <= 0 or ask < bid:
        return True
    spread = ask - bid
    if bid <= 0.05 or ask >= 0.95:
        return True
    return spread >= 0.20


def _should_ignore_transition_book(
    *,
    trade: LiveTradeState,
    current_slug: Optional[str],
    active_secs: Optional[int],
    bid: float,
    ask: float,
    cfg: Next1ScalpConfigV1,
) -> bool:
    if not trade.event_slug or not current_slug or trade.event_slug != current_slug:
        return False
    if active_secs is None:
        return False
    if active_secs < (300 - int(cfg.current_transition_ignore_secs)):
        return False
    return _book_is_extreme_or_invalid(bid, ask)


def _is_flat_qty(qty: float, epsilon: float = 0.000001) -> bool:
    return abs(float(qty)) <= float(epsilon)


def _is_match_status(status: Optional[str]) -> bool:
    return str(status or "").lower() in ("matched", "filled", "closed", "resolved")


def _has_confirmed_entry_balance(broker, trade: LiveTradeState) -> bool:
    token_balance_qty = _token_balance_qty(broker, trade.token_id)
    return token_balance_qty >= max(0.000001, trade.total_entry_qty * 0.5)


def _entry_confirm_deadline(trade: LiveTradeState, now: float, seconds: float = 2.0, polls: int = 2) -> bool:
    return (trade.confirm_started_at > 0 and (now - trade.confirm_started_at) >= seconds) or trade.confirm_polls >= polls


def _exit_confirm_deadline(trade: LiveTradeState, now: float, seconds: float = 2.0, polls: int = 2) -> bool:
    return (trade.confirm_started_at > 0 and (now - trade.confirm_started_at) >= seconds) or trade.confirm_polls >= polls


def _handoff_secs_for_trade(
    trade: LiveTradeState,
    *,
    current_slug: Optional[str],
    next1_slug: Optional[str],
    current_secs: Optional[int],
    active_secs: Optional[int],
) -> Optional[int]:
    if not trade.event_slug:
        return None
    if trade.event_slug == current_slug:
        return 0
    if trade.event_slug == next1_slug:
        return current_secs
    return active_secs


def _desired_aggressive_reprice(trade: LiveTradeState, signal: dict, tick_size: float, cfg: Next1ScalpConfigV1) -> float:
    signal_price = _safe_float(signal.get("aggressive_entry_price"), 0.0)
    current_price = _safe_float(trade.aggressive_entry_price, 0.0)
    if signal_price <= 0:
        return 0.0
    if current_price <= 0:
        return min(signal_price, _entry_price_cap(signal, cfg))
    max_step_up = round(current_price + tick_size, 6)
    return round(min(signal_price, _entry_price_cap(signal, cfg), max_step_up), 6)


def _replace_entry_order(
    broker,
    *,
    trade: LiveTradeState,
    leg: str,
    price: float,
    now: float,
) -> Optional[dict]:
    remaining_qty = _entry_leg_remaining_qty(trade, leg)
    if remaining_qty <= 0 or not trade.token_id or not trade.side:
        return None

    old_order_id = trade.aggressive_order_id if leg == "aggressive" else trade.passive_order_id
    cancel_resp = _cancel_if_live(broker, old_order_id)
    client_key = f"next1_scalp:entry_{leg}:repriced:{int(now)}:{trade.side}"
    req = BrokerOrderRequest(
        token_id=trade.token_id,
        side="BUY",
        price=float(price),
        size=float(remaining_qty),
        market_slug=trade.event_slug,
        outcome=trade.side,
        client_order_key=client_key,
    )
    order = broker.place_limit_order(req)
    if leg == "aggressive":
        trade.aggressive_order_id = order.order_id
        trade.aggressive_entry_price = float(price)
    else:
        trade.passive_order_id = order.order_id
        trade.passive_entry_price = float(price)
    trade.last_entry_reprice_at = now
    trade.updated_at = now
    trade.last_reason = f"{leg}_repriced"
    return {"cancel": cancel_resp, "order": order.as_dict(), "leg": leg, "price": float(price), "remaining_qty": remaining_qty}


def _post_exit_order(
    broker,
    trade: LiveTradeState,
    *,
    exit_price: float,
    now: float,
    reason: str,
    min_limit_exit_qty: float,
) -> LiveTradeState:
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
            trade.last_reason = f"close_failed_residual_position:{round(qty, 6)}:{type(exc).__name__}"
            return trade
    order_type = "GTC"
    post_price = float(exit_price)
    if qty < float(min_limit_exit_qty):
        order_type = "FAK"
        # For dust-sized residuals, cross the book aggressively instead of anchoring to the current bid.
        post_price = 0.01
    req = BrokerOrderRequest(
        token_id=trade.token_id or "",
        side="SELL",
        price=post_price,
        size=float(qty),
        order_type=order_type,
        market_slug=trade.event_slug,
        outcome=trade.side,
        client_order_key=f"next1_scalp:exit:{reason}:{int(now)}:{trade.side}",
    )
    try:
        order = broker.place_limit_order(req)
        trade.exit_order_id = order.order_id
        trade.mode = "pending_exit"
        trade.updated_at = now
        trade.last_reason = f"exit_posted:{reason}:{order_type.lower()}"
    except Exception as exc:
        trade.mode = "pending_exit"
        trade.exit_order_id = None
        trade.updated_at = now
        trade.last_reason = f"close_failed_residual_position:{round(qty, 6)}:{type(exc).__name__}"
    return trade


def _force_risk_cleanup(broker, trade: LiveTradeState, log_path: Path, now: float, reason: str) -> None:
    try:
        _append_jsonl(log_path, {"type": "panic", "ts": now, "reason": reason, "trade": asdict(trade)})
        _cancel_if_live(broker, trade.passive_order_id)
        _cancel_if_live(broker, trade.aggressive_order_id)
        _cancel_if_live(broker, trade.exit_order_id)
        token_balance_qty = _token_balance_qty(broker, trade.token_id)
        panic_qty = token_balance_qty if token_balance_qty > 0 else trade.remaining_position_qty
        if not _is_flat_qty(panic_qty) and trade.token_id and trade.side:
            active_book = _fetch_active_book(trade)
            panic_bid = _best_bid(active_book or {})
            if panic_bid > 0:
                try:
                    broker.update_balance_allowance(asset_type="CONDITIONAL", token_id=trade.token_id)
                except Exception:
                    pass
                req = BrokerOrderRequest(
                    token_id=trade.token_id,
                    side="SELL",
                    price=float(panic_bid),
                    size=float(panic_qty),
                    order_type="FAK" if panic_qty < 5.0 else "GTC",
                    market_slug=trade.event_slug,
                    outcome=trade.side,
                    client_order_key=f"next1_scalp:panic_exit:{int(now)}:{trade.side}",
                )
                order = broker.place_limit_order(req)
                _append_jsonl(log_path, {"type": "panic_exit_posted", "ts": now, "price": panic_bid, "order": order.as_dict(), "trade": asdict(trade)})
    except Exception as exc:
        _append_jsonl(log_path, {"type": "panic_error", "ts": now, "reason": reason, "error": f"{type(exc).__name__}: {exc}"})


def _shutdown_reconcile(
    broker,
    trade: LiveTradeState,
    *,
    min_limit_exit_qty: float,
    state_path: Path,
    log_path: Path,
    session_id: str,
    now: float,
) -> LiveTradeState:
    try:
        reconciled = _restore_trade_from_broker(broker, trade)
    except Exception:
        reconciled = trade
    open_orders = []
    try:
        open_orders = [o.as_dict() for o in broker.get_open_orders()[:50]]
    except Exception:
        open_orders = []
    token_balance = _token_balance_qty(broker, reconciled.token_id)
    if reconciled.mode == "idle" and not open_orders and _is_flat_qty(token_balance):
        _append_trade_event(
            log_path,
            event_type="shutdown_flat",
            session_id=session_id,
            now=now,
            trade=reconciled,
            extra={"open_orders": open_orders, "token_balance_qty": token_balance},
        )
        _clear_state(state_path)
        return LiveTradeState()
    if (
        not _is_flat_qty(token_balance)
        and not open_orders
        and reconciled.token_id
        and reconciled.side
        and reconciled.event_slug
    ):
        reconciled = _post_exit_order(
            broker,
            reconciled,
            exit_price=0.01,
            now=now,
            reason="shutdown_dust_flush",
            min_limit_exit_qty=min_limit_exit_qty,
        )
        try:
            open_orders = [o.as_dict() for o in broker.get_open_orders()[:50]]
        except Exception:
            open_orders = []
        token_balance = _token_balance_qty(broker, reconciled.token_id)
        if reconciled.mode == "idle" and not open_orders and _is_flat_qty(token_balance):
            _append_trade_event(
                log_path,
                event_type="shutdown_flat",
                session_id=session_id,
                now=now,
                trade=reconciled,
                extra={"open_orders": open_orders, "token_balance_qty": token_balance, "dust_flush": True},
            )
            _clear_state(state_path)
            return LiveTradeState()
    _save_state(state_path, reconciled)
    _append_trade_event(
        log_path,
        event_type="shutdown_non_idle",
        session_id=session_id,
        now=now,
        trade=reconciled,
        extra={"open_orders": open_orders, "token_balance_qty": token_balance},
    )
    return reconciled


def _attempt_restore_residual_flush(
    broker,
    trade: LiveTradeState,
    *,
    min_limit_exit_qty: float,
    log_path: Path,
    session_id: str,
    now: float,
) -> LiveTradeState:
    token_balance = _token_balance_qty(broker, trade.token_id)
    if trade.mode == "idle" or _is_flat_qty(token_balance) or not trade.token_id or not trade.side or not trade.event_slug:
        return trade
    flushed = _post_exit_order(
        broker,
        trade,
        exit_price=0.01,
        now=now,
        reason="restore_dust_flush",
        min_limit_exit_qty=min_limit_exit_qty,
    )
    _append_trade_event(
        log_path,
        event_type="restore_dust_flush_posted",
        session_id=session_id,
        now=now,
        trade=flushed,
        extra={"token_balance_qty": token_balance},
    )
    time.sleep(1.0)
    refreshed = _restore_trade_from_broker(broker, flushed)
    refreshed_balance = _token_balance_qty(broker, refreshed.token_id)
    _append_trade_event(
        log_path,
        event_type="restore_dust_flush_result",
        session_id=session_id,
        now=time.time(),
        trade=refreshed,
        extra={"token_balance_qty": refreshed_balance},
    )
    if refreshed.mode == "idle" and _is_flat_qty(refreshed_balance):
        return LiveTradeState()
    return refreshed


def _archive_resolved_untradeable_residual(
    trade: LiveTradeState,
    *,
    token_balance_qty: float,
    secs_to_end: Optional[int],
    session_id: str,
    now: float,
) -> None:
    _append_jsonl(
        _residuals_path(),
        {
            "type": "resolved_untradeable_residual",
            "ts": now,
            "session_id": session_id,
            "trade": _trade_summary(trade),
            "token_balance_qty": token_balance_qty,
            "secs_to_end": secs_to_end,
        },
    )


def monitor_live_next1_scalp_real_v1(duration_seconds: Optional[int] = None, log_dir: Optional[str] = None) -> None:
    load_dotenv()
    guarded_cfg = load_live_guarded_config()
    broker_status = load_broker_env()
    cfg = Next1ScalpConfigV1()

    print("[BROKER_ENV]", broker_status.as_dict())
    print("[LIVE_GUARDED_CONFIG]", guarded_cfg.as_dict())
    print("[NEXT1_SCALP_CONFIG]", cfg.as_dict())

    if not guarded_cfg.enabled:
        print("[GUARD] Set POLY_GUARDED_ENABLED=true")
        return
    if guarded_cfg.shadow_only:
        print("[GUARD] Set POLY_GUARDED_SHADOW_ONLY=false")
        return
    if not guarded_cfg.real_posts_enabled:
        print("[GUARD] Set POLY_GUARDED_REAL_POSTS_ENABLED=true")
        return
    if not _env_bool("POLY_NEXT1_SCALP_REAL_ENABLED", False):
        print("[GUARD] Set POLY_NEXT1_SCALP_REAL_ENABLED=true to arm real next1 scalp")
        return
    if not broker_status.ready_for_real_smoke:
        print("[GUARD] Broker env missing required credentials")
        return

    aggressive_qty = _env_int("POLY_NEXT1_SCALP_AGGRESSIVE_QTY", 0)
    passive_qty = _env_int("POLY_NEXT1_SCALP_PASSIVE_QTY", 6)
    entry_timeout_secs = _env_int("POLY_NEXT1_SCALP_ENTRY_TIMEOUT_SECS", 25)
    entry_reprice_secs = max(1, _env_int("POLY_NEXT1_SCALP_ENTRY_REPRICE_SECS", 1))
    exit_repost_secs = _env_int("POLY_NEXT1_SCALP_EXIT_REPOST_SECS", 6)
    min_limit_exit_qty = _env_float("POLY_NEXT1_SCALP_MIN_LIMIT_EXIT_QTY", 5.0)
    run_for = int(duration_seconds or _env_int("POLY_NEXT1_SCALP_RUN_SECONDS", 14400))
    poll_secs = max(0.5, float(os.getenv("POLY_NEXT1_SCALP_POLL_SECS", "0.5")))
    session_dir = Path(log_dir) if log_dir else _build_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "next1_scalp_real.jsonl"
    exception_path = session_dir / "exception.log"
    state_path = _state_path()
    session_id = session_dir.name

    print(
        "[NEXT1_SCALP_REAL_PARAMS]",
        {
            "aggressive_qty": aggressive_qty,
            "passive_qty": passive_qty,
            "entry_timeout_secs": entry_timeout_secs,
            "entry_reprice_secs": entry_reprice_secs,
            "exit_repost_secs": exit_repost_secs,
            "min_limit_exit_qty": min_limit_exit_qty,
            "run_for": run_for,
            "poll_secs": poll_secs,
            "log_path": str(log_path),
            "state_path": str(state_path),
        },
    )

    broker = PolymarketBrokerV3.from_env()
    startup_executor = Setup1BrokerExecutorV4(broker=broker, shadow_only=False, min_shares_per_leg=guarded_cfg.min_shares_per_leg)
    health = broker.healthcheck()
    print("[BROKER_HEALTH]", health.as_dict())
    if not health.ok:
        print("[GUARD] Broker healthcheck failed")
        return

    startup_orders = broker.get_open_orders()[:50]
    print("[BROKER_OPEN_ORDERS_STARTUP]", [o.as_dict() for o in startup_orders])
    allowed, startup_report = evaluate_startup_guard(startup_executor, startup_orders)
    print("[STARTUP_GUARD]", startup_report)
    restored_trade = _load_state(state_path) or LiveTradeState()
    _append_jsonl(
        log_path,
        {
            "type": "startup",
            "ts": time.time(),
            "session_id": session_id,
            "startup_orders": [o.as_dict() for o in startup_orders],
            "startup_guard": startup_report,
            "restored_trade": _trade_summary(restored_trade),
        },
    )

    if restored_trade.mode != "idle":
        restored_trade = _restore_trade_from_broker(broker, restored_trade)
        print("[RESTORED_TRADE]", asdict(restored_trade))
        _append_trade_event(log_path, event_type="restore_result", session_id=session_id, now=time.time(), trade=restored_trade)
        if restored_trade.mode == "idle":
            _clear_state(state_path)
        else:
            restored_trade = _attempt_restore_residual_flush(
                broker,
                restored_trade,
                min_limit_exit_qty=min_limit_exit_qty,
                log_path=log_path,
                session_id=session_id,
                now=time.time(),
            )
            print("[RESTORE_AFTER_FLUSH]", asdict(restored_trade))
            if restored_trade.mode == "idle":
                _clear_state(state_path)
            else:
                _save_state(state_path, restored_trade)
            restored_balance = _token_balance_qty(broker, restored_trade.token_id)
            restored_secs = _seconds_to_end_from_slug(restored_trade.event_slug, time.time())
            if restored_trade.mode != "idle":
                if restored_secs == 0 and not _is_flat_qty(restored_balance):
                    _archive_resolved_untradeable_residual(
                        restored_trade,
                        token_balance_qty=restored_balance,
                        secs_to_end=restored_secs,
                        session_id=session_id,
                        now=time.time(),
                    )
                    _append_trade_event(
                        log_path,
                        event_type="restore_archived_resolved_residual",
                        session_id=session_id,
                        now=time.time(),
                        trade=restored_trade,
                        extra={"token_balance": restored_balance, "secs_to_end": restored_secs},
                    )
                    print(
                        "[RESTORE] Archived resolved residual and cleared active state. "
                        f"token_balance={restored_balance} event_slug={restored_trade.event_slug}"
                    )
                    restored_trade = LiveTradeState()
                    _clear_state(state_path)
                if restored_trade.mode != "idle":
                    print(
                        "[GUARD] Refusing to start with restored non-idle state. "
                        f"token_balance={restored_balance} event_slug={restored_trade.event_slug} secs_to_end={restored_secs}"
                    )
                    _append_trade_event(
                        log_path,
                        event_type="restore_blocked_non_idle",
                        session_id=session_id,
                        now=time.time(),
                        trade=restored_trade,
                        extra={"token_balance": restored_balance, "secs_to_end": restored_secs},
                    )
                    return

    if not allowed:
        print("[GUARD] Refusing to start due to blocked startup guard.")
        return
    if startup_orders and restored_trade.mode == "idle":
        print("[GUARD] Refusing to start with open orders not owned by restored scalp state.")
        return

    research = Next1ScalpResearchV1(cfg=cfg)
    trade = restored_trade
    started_at = time.time()

    while time.time() - started_at < run_for:
        now = time.time()
        try:
            slot_bundle = _build_slot_bundle()
            current_item = slot_bundle["queue"].get("current")
            next1_item = slot_bundle["queue"].get("next_1")
            current_slug = current_item.get("slug") if current_item else None
            current_secs = int(current_item.get("seconds_to_end")) if current_item and current_item.get("seconds_to_end") is not None else None
            next1_secs = int(next1_item.get("seconds_to_end")) if next1_item and next1_item.get("seconds_to_end") is not None else None
            slot_state = _fetch_slot_state(slot_bundle)
            current_snap = _slot_snapshot(slot_state, "current")
            next1_snap = _slot_snapshot(slot_state, "next_1")
            next1_exec, next1_exec_reason = _compute_executable_metrics(next1_snap)
            reference = fetch_external_btc_reference_v1()
            signal = research.evaluate(
                current_snap=current_snap,
                next1_snap=next1_snap,
                current_secs=current_secs,
                next1_secs=next1_secs,
                reference_price=reference.get("reference_price"),
                source_divergence_bps=reference.get("source_divergence_bps"),
                now_ts=now,
            )
            if next1_item:
                signal["event_slug"] = next1_item["slug"]

            active_book = _fetch_active_book(trade) if trade.mode in ("working_entry", "entry_pending_confirm", "open_position", "pending_exit", "exit_pending_confirm") else None
            active_bid = _best_bid(active_book or {})
            active_ask = _best_ask(active_book or {})
            active_secs = _seconds_to_end_from_slug(trade.event_slug, now) if trade.event_slug else None
            next1_slug = next1_item.get("slug") if next1_item else None
            handoff_secs = _handoff_secs_for_trade(
                trade,
                current_slug=current_slug,
                next1_slug=next1_slug,
                current_secs=current_secs,
                active_secs=active_secs,
            )
            active_tick_size = _tick_size_from_book(active_book or {}, _tick_size_from_snap(next1_snap, trade.side or "UP"))
            if trade.event_slug and trade.side and next1_item and next1_item.get("slug") == trade.event_slug:
                exec_bid = _bid_for_side(next1_exec, trade.side)
                exec_ask = _safe_float(next1_exec.get("up_ask" if trade.side == "UP" else "down_ask"), 0.0) if next1_exec else 0.0
                if exec_bid > 0:
                    active_bid = max(active_bid, exec_bid)
                if exec_ask > 0:
                    active_ask = min(active_ask, exec_ask) if active_ask > 0 else exec_ask
            if trade.event_slug and trade.side and signal.get("event_slug") == trade.event_slug:
                signal_exit_price = _safe_float(signal.get("exit_price"), 0.0)
                signal_entry_price = _safe_float(signal.get("aggressive_entry_price"), 0.0)
                if signal_exit_price > 0:
                    active_bid = max(active_bid, signal_exit_price)
                if signal_entry_price > 0:
                    active_ask = signal_entry_price

            snapshot = {
                "type": "snapshot",
                "ts": now,
                "session_id": session_id,
                "current_slug": current_slug,
                "next1_slug": next1_item.get("slug") if next1_item else None,
                "current_secs": current_secs,
                "next1_secs": next1_secs,
                "next1_exec_reason": next1_exec_reason,
                "signal": _signal_summary(signal),
                "trade": _trade_summary(trade),
                "active_market": {
                    "event_slug": trade.event_slug,
                    "secs_to_end": active_secs,
                    "bid": active_bid,
                    "ask": active_ask,
                },
            }
            _append_jsonl(log_path, snapshot)
            print(
                f"[NEXT1_SCALP_REAL] next1_secs={next1_secs} active_secs={active_secs} setup={signal.get('setup')} "
                f"allow={signal.get('allow')} side={signal.get('side')} mode={trade.mode} qty={trade.total_entry_qty}/{trade.remaining_position_qty}"
            )

            if trade.mode == "idle" and signal.get("allow") and next1_item and next1_secs is not None and next1_secs >= cfg.min_secs_to_end:
                trade = _post_entry_orders(
                    broker,
                    signal=signal,
                    snap=next1_snap,
                    now=now,
                    aggressive_qty=aggressive_qty,
                    passive_qty=passive_qty,
                )
                _save_state(state_path, trade)
                _append_trade_event(
                    log_path,
                    event_type="enter",
                    session_id=session_id,
                    now=now,
                    trade=trade,
                    signal=signal,
                    extra={
                        "entry_orders": {
                            "aggressive": _order_snapshot(_get_order_status(broker, trade.aggressive_order_id)),
                            "passive": _order_snapshot(_get_order_status(broker, trade.passive_order_id)),
                        }
                    },
                )
                time.sleep(poll_secs)
                continue

            if trade.mode in ("working_entry", "entry_pending_confirm", "open_position", "pending_exit", "exit_pending_confirm"):
                trade.aggressive_qty_filled = _sync_entry_order(
                    broker,
                    trade,
                    order_id=trade.aggressive_order_id,
                    current_filled=trade.aggressive_qty_filled,
                )
                trade.passive_qty_filled = _sync_entry_order(
                    broker,
                    trade,
                    order_id=trade.passive_order_id,
                    current_filled=trade.passive_qty_filled,
                )
                trade.entry_price_avg = _average_entry_price(trade)
                trade = _reconcile_position_to_token_balance(broker, trade)
                trade = _rebuild_levels(trade, tick_size=active_tick_size, cfg=cfg)
                trade.updated_at = now
                _save_state(state_path, trade)

            if trade.mode == "working_entry":
                signal_valid = _entry_signal_still_valid(trade, signal, active_secs, cfg)
                should_cancel_unfilled = (
                    (not signal_valid)
                    or now - trade.created_at >= entry_timeout_secs
                    or (handoff_secs is not None and handoff_secs <= cfg.handoff_cancel_secs)
                )

                if trade.total_entry_qty > 0:
                    trade.mode = "entry_pending_confirm"
                    trade.last_reason = "entry_fill_detected_api"
                    trade.confirm_started_at = now
                    trade.confirm_polls = 1
                    _save_state(state_path, trade)
                    _append_trade_event(
                        log_path,
                        event_type="fill_detected_api",
                        session_id=session_id,
                        now=now,
                        trade=trade,
                        signal=signal,
                        extra={
                            "aggressive_order": _order_snapshot(_get_order_status(broker, trade.aggressive_order_id)),
                            "passive_order": _order_snapshot(_get_order_status(broker, trade.passive_order_id)),
                        },
                    )
                    time.sleep(poll_secs)
                    continue
                elif (
                    signal_valid
                    and active_ask > 0
                    and not _book_is_extreme_or_invalid(active_bid, active_ask)
                    and now - trade.last_entry_reprice_at >= entry_reprice_secs
                ):
                    desired_aggressive = _desired_aggressive_reprice(trade, signal, active_tick_size, cfg)
                    desired_passive = _adaptive_passive_entry_price(desired_aggressive, active_ask, active_tick_size)
                    reprice_events = []

                    if (
                        trade.aggressive_order_id
                        and _entry_leg_remaining_qty(trade, "aggressive") > 0
                        and desired_aggressive > 0
                        and abs(_safe_float(trade.aggressive_entry_price) - desired_aggressive) >= active_tick_size
                    ):
                        event = _replace_entry_order(
                            broker,
                            trade=trade,
                            leg="aggressive",
                            price=desired_aggressive,
                            now=now,
                        )
                        if event is not None:
                            reprice_events.append(event)

                    if (
                        trade.passive_order_id
                        and _entry_leg_remaining_qty(trade, "passive") > 0
                        and desired_passive > 0
                        and abs(_safe_float(trade.passive_entry_price) - desired_passive) >= active_tick_size
                    ):
                        event = _replace_entry_order(
                            broker,
                            trade=trade,
                            leg="passive",
                            price=desired_passive,
                            now=now,
                        )
                        if event is not None:
                            reprice_events.append(event)

                    if reprice_events:
                        _save_state(state_path, trade)
                        _append_trade_event(
                            log_path,
                            event_type="entry_reprice",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={"events": reprice_events},
                        )

                if should_cancel_unfilled:
                    resp_passive = None
                    resp_aggressive = None
                    if trade.passive_qty_filled <= 0:
                        resp_passive = _cancel_if_live(broker, trade.passive_order_id)
                        trade.passive_order_id = None
                    if trade.aggressive_qty_filled <= 0:
                        resp_aggressive = _cancel_if_live(broker, trade.aggressive_order_id)
                        trade.aggressive_order_id = None

                    if trade.total_entry_qty <= 0:
                        _append_jsonl(
                            log_path,
                            {
                                "type": "entry_cancel",
                                "ts": now,
                                "session_id": session_id,
                                "aggressive": resp_aggressive,
                                "passive": resp_passive,
                                "trade": _trade_summary(trade),
                                "signal": _signal_summary(signal),
                            },
                        )
                        trade = LiveTradeState()
                        _clear_state(state_path)
                    else:
                        if resp_passive is not None:
                            _append_trade_event(
                                log_path,
                                event_type="passive_cancel",
                                session_id=session_id,
                                now=now,
                                trade=trade,
                                signal=signal,
                                extra={"response": resp_passive},
                            )
                        trade.mode = "entry_pending_confirm"
                        trade.last_reason = "partial_fill_detected_api"
                        trade.confirm_started_at = now
                        trade.confirm_polls = 1
                        _save_state(state_path, trade)
                        _append_trade_event(
                            log_path,
                            event_type="partial_fill_detected_api",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={
                                "aggressive_order": _order_snapshot(_get_order_status(broker, trade.aggressive_order_id)),
                                "passive_order": _order_snapshot(_get_order_status(broker, trade.passive_order_id)),
                            },
                        )
                        time.sleep(poll_secs)
                        continue

            if trade.mode == "entry_pending_confirm":
                entry_order = _get_order_status(broker, trade.aggressive_order_id)
                entry_status = str(getattr(entry_order, "status", "") or "").lower()
                entry_balance_confirmed = _has_confirmed_entry_balance(broker, trade)
                if handoff_secs is not None and handoff_secs <= cfg.handoff_cancel_secs:
                    if entry_balance_confirmed or trade.total_entry_qty > 0:
                        trade.mode = "open_position"
                        trade.last_reason = "entry_fill_confirmed_handoff"
                        trade.confirm_started_at = 0.0
                        trade.confirm_polls = 0
                        _save_state(state_path, trade)
                    else:
                        _append_trade_event(
                            log_path,
                            event_type="fill_reverted_handoff",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={"aggressive_order": _order_snapshot(entry_order), "entry_status": entry_status, "handoff_secs": handoff_secs},
                        )
                        trade = LiveTradeState()
                        _clear_state(state_path)
                        time.sleep(poll_secs)
                        continue
                if trade.mode != "entry_pending_confirm":
                    pass
                elif entry_balance_confirmed:
                    trade.mode = "open_position"
                    trade.last_reason = "entry_fill_confirmed"
                    trade.confirm_started_at = 0.0
                    trade.confirm_polls = 0
                    _save_state(state_path, trade)
                    _append_trade_event(
                        log_path,
                        event_type="fill_confirmed",
                        session_id=session_id,
                        now=now,
                        trade=trade,
                        signal=signal,
                        extra={"aggressive_order": _order_snapshot(entry_order)},
                    )
                    time.sleep(poll_secs)
                    continue
                trade.confirm_polls += 1
                if _entry_confirm_deadline(trade, now):
                    if not _is_match_status(entry_status) and _is_flat_qty(_token_balance_qty(broker, trade.token_id)):
                        _append_trade_event(
                            log_path,
                            event_type="fill_reverted",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={"aggressive_order": _order_snapshot(entry_order), "entry_status": entry_status},
                        )
                        trade = LiveTradeState()
                        _clear_state(state_path)
                        time.sleep(poll_secs)
                        continue
                    trade.mode = "open_position"
                    trade.last_reason = "entry_fill_unconfirmed_timeout"
                    trade.confirm_started_at = 0.0
                    trade.confirm_polls = 0
                    _save_state(state_path, trade)
                    _append_trade_event(
                        log_path,
                        event_type="fill_unconfirmed_timeout",
                        session_id=session_id,
                        now=now,
                        trade=trade,
                        signal=signal,
                        extra={"aggressive_order": _order_snapshot(entry_order), "entry_status": entry_status},
                    )
                    time.sleep(poll_secs)
                    continue

            if trade.mode == "open_position" and trade.side:
                bid_now = active_bid
                ignore_transition_book = _should_ignore_transition_book(
                    trade=trade,
                    current_slug=current_slug,
                    active_secs=active_secs,
                    bid=active_bid,
                    ask=active_ask,
                    cfg=cfg,
                )
                if ignore_transition_book:
                    _append_trade_event(
                        log_path,
                        event_type="transition_book_ignored",
                        session_id=session_id,
                        now=now,
                        trade=trade,
                        signal=signal,
                        extra={
                            "current_slug": current_slug,
                            "active_secs": active_secs,
                            "bid_now": active_bid,
                            "ask_now": active_ask,
                        },
                    )
                    time.sleep(poll_secs)
                    continue
                trade.best_bid = max(_safe_float(trade.best_bid, 0.0), bid_now)
                if trade.entry_price_avg is not None and trade.best_bid >= round(float(trade.entry_price_avg) + active_tick_size, 6):
                    trade.stop_price = max(_safe_float(trade.stop_price), round(float(trade.best_bid) - active_tick_size, 6))

                if trade.passive_order_id and trade.passive_qty_filled <= 0:
                    passive_should_cancel = (
                        (active_secs is not None and active_secs < cfg.min_secs_to_end)
                        or now - trade.created_at >= entry_timeout_secs
                    )
                    passive_signal_valid = _entry_signal_still_valid(trade, signal, active_secs, cfg)
                    if (
                        not passive_should_cancel
                        and passive_signal_valid
                        and active_ask > 0
                        and not _book_is_extreme_or_invalid(active_bid, active_ask)
                        and now - trade.last_entry_reprice_at >= entry_reprice_secs
                        and _entry_leg_remaining_qty(trade, "passive") > 0
                    ):
                        desired_aggressive = _desired_aggressive_reprice(trade, signal, active_tick_size, cfg)
                        desired_passive = _adaptive_passive_entry_price(desired_aggressive, active_ask, active_tick_size)
                        if abs(_safe_float(trade.passive_entry_price) - desired_passive) >= active_tick_size:
                            event = _replace_entry_order(
                                broker,
                                trade=trade,
                                leg="passive",
                                price=desired_passive,
                                now=now,
                            )
                            if event is not None:
                                _save_state(state_path, trade)
                                _append_trade_event(
                                    log_path,
                                    event_type="passive_reprice",
                                    session_id=session_id,
                                    now=now,
                                    trade=trade,
                                    signal=signal,
                                    extra={"event": event},
                                )
                    elif passive_should_cancel or not passive_signal_valid:
                        resp = _cancel_if_live(broker, trade.passive_order_id)
                        trade.passive_order_id = None
                        _append_trade_event(
                            log_path,
                            event_type="passive_cancel",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={"response": resp},
                        )

                if bid_now > 0:
                    exit_reason = None
                    if handoff_secs is not None and handoff_secs <= cfg.handoff_cancel_secs:
                        exit_reason = "handoff_precurrent"
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=bid_now,
                            now=now,
                            reason=exit_reason,
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                    elif active_secs is not None and active_secs <= cfg.flatten_deadline_secs:
                        exit_reason = "handoff_deadline"
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=bid_now,
                            now=now,
                            reason=exit_reason,
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                    elif bid_now >= _safe_float(trade.target_price):
                        exit_reason = "target"
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=bid_now,
                            now=now,
                            reason=exit_reason,
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                    elif bid_now <= _safe_float(trade.stop_price):
                        exit_reason = "stop"
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=bid_now,
                            now=now,
                            reason=exit_reason,
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                    elif active_secs is not None and active_secs <= 5:
                        exit_reason = "deadline"
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=bid_now,
                            now=now,
                            reason=exit_reason,
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                    elif now - trade.created_at >= cfg.max_hold_secs:
                        exit_reason = "timeout"
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=bid_now,
                            now=now,
                            reason=exit_reason,
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                    if trade.mode == "pending_exit":
                        _save_state(state_path, trade)
                        _append_trade_event(
                            log_path,
                            event_type="exit_posted",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={
                                "exit_reason": exit_reason,
                                "exit_order": _order_snapshot(_get_order_status(broker, trade.exit_order_id)),
                                "bid_now": bid_now,
                                "post_result": str(trade.last_reason or ""),
                            },
                        )

            if trade.mode == "pending_exit":
                token_balance_qty = _token_balance_qty(broker, trade.token_id)
                exit_order = _get_order_status(broker, trade.exit_order_id)
                if exit_order is None:
                    if _is_flat_qty(token_balance_qty):
                        _append_trade_event(
                            log_path,
                            event_type="flat",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={"exit_order": None, "exit_status": None, "token_balance_qty": token_balance_qty},
                        )
                        trade = LiveTradeState()
                        _clear_state(state_path)
                    elif (
                        active_secs is not None
                        and active_secs <= cfg.flatten_deadline_secs
                        and trade.side
                        and (active_bid > 0 or token_balance_qty < min_limit_exit_qty)
                    ):
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=active_bid if active_bid > 0 else 0.01,
                            now=now,
                            reason="handoff_deadline_retry",
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                        _save_state(state_path, trade)
                        _append_trade_event(
                            log_path,
                            event_type="exit_repost",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={
                                "residual_qty": round(token_balance_qty, 6),
                                "exit_reason": "handoff_deadline_retry",
                                "bid_now": active_bid,
                                "new_exit_order": _order_snapshot(_get_order_status(broker, trade.exit_order_id)),
                            },
                        )
                    elif now - trade.updated_at >= exit_repost_secs and trade.side and (active_bid > 0 or token_balance_qty < min_limit_exit_qty):
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=active_bid if active_bid > 0 else 0.01,
                            now=now,
                            reason="retry_residual",
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                        _save_state(state_path, trade)
                        event_type = (
                            "close_failed_residual_position"
                            if str(trade.last_reason or "").startswith("close_failed_residual_position:")
                            else "exit_repost"
                        )
                        residual_qty = round(token_balance_qty, 6)
                        _append_trade_event(
                            log_path,
                            event_type=event_type,
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={
                                "residual_qty": residual_qty,
                                "exit_reason": "retry_residual",
                                "bid_now": active_bid,
                                "new_exit_order": _order_snapshot(_get_order_status(broker, trade.exit_order_id)),
                            },
                        )
                else:
                    trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
                    status = str(getattr(exit_order, "status", "") or "").lower()
                    if _is_flat_qty(token_balance_qty) and _is_match_status(status):
                        _append_trade_event(
                            log_path,
                            event_type="flat",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={
                                "exit_order": _order_snapshot(exit_order),
                                "exit_status": status,
                                "token_balance_qty": token_balance_qty,
                            },
                        )
                        trade = LiveTradeState()
                        _clear_state(state_path)
                    elif _is_flat_qty(token_balance_qty):
                        _append_trade_event(
                            log_path,
                            event_type="flat",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={"exit_order": _order_snapshot(exit_order), "exit_status": status},
                        )
                        trade = LiveTradeState()
                        _clear_state(state_path)
                    elif _is_match_status(status) and token_balance_qty > 0:
                        trade.mode = "exit_pending_confirm"
                        trade.last_reason = f"exit_match_pending_confirm:{round(token_balance_qty, 6)}"
                        trade.confirm_started_at = now
                        trade.confirm_polls = 1
                        _save_state(state_path, trade)
                        _append_trade_event(
                            log_path,
                            event_type="exit_match_pending_confirm",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={
                                "exit_order": _order_snapshot(exit_order),
                                "exit_status": status,
                                "token_balance_qty": token_balance_qty,
                            },
                        )
                    elif now - trade.updated_at >= exit_repost_secs and trade.side and active_bid > 0:
                        cancel_resp = _cancel_if_live(broker, trade.exit_order_id)
                        trade.exit_order_id = None
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=active_bid,
                            now=now,
                            reason="repost",
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                        _save_state(state_path, trade)
                        if trade.mode == "pending_exit":
                            _append_trade_event(
                                log_path,
                                event_type="exit_repost",
                                session_id=session_id,
                                now=now,
                                trade=trade,
                                signal=signal,
                                extra={
                                    "cancel": cancel_resp,
                                    "new_exit_order": _order_snapshot(_get_order_status(broker, trade.exit_order_id)),
                                    "bid_now": active_bid,
                                },
                            )
                        elif str(trade.last_reason or "").startswith("close_failed_residual_position:"):
                            residual_qty = str(trade.last_reason).split(":", 2)[1]
                            _save_state(state_path, trade)
                            _append_trade_event(
                                log_path,
                                event_type="close_failed_residual_position",
                                session_id=session_id,
                                now=now,
                                trade=trade,
                                signal=signal,
                                extra={"residual_qty": residual_qty, "exit_reason": "repost", "bid_now": active_bid},
                            )

            if trade.mode == "exit_pending_confirm":
                token_balance_qty = _token_balance_qty(broker, trade.token_id)
                exit_order = _get_order_status(broker, trade.exit_order_id)
                exit_status = str(getattr(exit_order, "status", "") or "").lower() if exit_order is not None else ""
                if _is_flat_qty(token_balance_qty):
                    _append_trade_event(
                        log_path,
                        event_type="flat",
                        session_id=session_id,
                        now=now,
                        trade=trade,
                        signal=signal,
                        extra={"exit_order": _order_snapshot(exit_order), "exit_status": exit_status, "token_balance_qty": token_balance_qty},
                    )
                    trade = LiveTradeState()
                    _clear_state(state_path)
                    time.sleep(poll_secs)
                    continue
                trade.confirm_polls += 1
                if _exit_confirm_deadline(trade, now):
                    trade.mode = "pending_exit"
                    trade.exit_order_id = None
                    trade.updated_at = now - exit_repost_secs
                    trade.confirm_started_at = 0.0
                    trade.confirm_polls = 0
                    trade.last_reason = f"residual_position_after_exit:{round(token_balance_qty, 6)}"
                    _save_state(state_path, trade)
                    _append_trade_event(
                        log_path,
                        event_type="residual_position_after_exit",
                        session_id=session_id,
                        now=now,
                        trade=trade,
                        signal=signal,
                        extra={"exit_order": _order_snapshot(exit_order), "exit_status": exit_status, "token_balance_qty": token_balance_qty},
                    )
                    if token_balance_qty < min_limit_exit_qty and trade.side:
                        trade = _post_exit_order(
                            broker,
                            trade,
                            exit_price=0.01,
                            now=now,
                            reason="residual_dust_flush",
                            min_limit_exit_qty=min_limit_exit_qty,
                        )
                        _save_state(state_path, trade)
                        _append_trade_event(
                            log_path,
                            event_type="exit_repost",
                            session_id=session_id,
                            now=now,
                            trade=trade,
                            signal=signal,
                            extra={
                                "residual_qty": round(token_balance_qty, 6),
                                "exit_reason": "residual_dust_flush",
                                "bid_now": active_bid,
                                "new_exit_order": _order_snapshot(_get_order_status(broker, trade.exit_order_id)),
                            },
                        )

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
            _force_risk_cleanup(broker, trade, log_path, now, f"{type(exc).__name__}: {exc}")
            _save_state(state_path, trade)
            raise

    trade = _shutdown_reconcile(
        broker,
        trade,
        min_limit_exit_qty=min_limit_exit_qty,
        state_path=state_path,
        log_path=log_path,
        session_id=session_id,
        now=time.time(),
    )


if __name__ == "__main__":
    monitor_live_next1_scalp_real_v1()
