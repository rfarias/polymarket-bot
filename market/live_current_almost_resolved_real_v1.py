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


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_log_dir() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"current_almost_resolved_real_{ts}"


def _state_path() -> Path:
    return Path("logs") / "current_almost_resolved_real_state.json"


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
) -> LiveCurrentAlmostResolvedTradeState:
    side = str(signal.get("side") or "")
    entry_price = _safe_float(signal.get("entry_price"), 0.0)
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
    post_price = float(exit_price) if exit_price > 0 else 0.01
    req = BrokerOrderRequest(
        token_id=trade.token_id or "",
        side="SELL",
        price=post_price,
        size=float(qty),
        order_type="GTC" if qty >= float(min_limit_exit_qty) else "FAK",
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

    qty = _env_int("POLY_CURRENT_ALMOST_RESOLVED_QTY", 5)
    entry_timeout_secs = _env_float("POLY_CURRENT_ALMOST_RESOLVED_ENTRY_TIMEOUT_SECS", 2.0)
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

    print(
        "[CURRENT_ALMOST_RESOLVED_REAL_PARAMS]",
        {
            "qty": qty,
            "entry_timeout_secs": entry_timeout_secs,
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

            active_book = _fetch_active_book(trade) if trade.mode in ("pending_entry", "open_position", "pending_exit", "exit_pending_confirm") else None
            active_bid = _best_bid(active_book or {})
            if trade.side and current_exec:
                exec_bid = _bid_for_side(current_exec, trade.side)
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
                trade = _post_entry_order(
                    broker,
                    signal=signal,
                    snap=current_snap,
                    qty=qty,
                    tick_size=tick_size,
                    now=now,
                    cfg=signal_cfg,
                )
                _save_state(state_path, trade)
                _append_jsonl(log_path, {"type": "enter", "ts": now, "session_id": session_id, "signal": signal, "trade": _trade_summary(trade)})
                time.sleep(poll_secs)
                continue

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
                    _append_jsonl(log_path, {"type": "fill", "ts": now, "session_id": session_id, "cancel_remainder": resp, "trade": _trade_summary(trade)})
                elif now - trade.created_at >= entry_timeout_secs or (current_secs is not None and current_secs <= signal_cfg.min_secs_to_end):
                    resp = _cancel_if_live(broker, trade.entry_order_id)
                    _append_jsonl(log_path, {"type": "entry_cancel", "ts": now, "session_id": session_id, "response": resp, "trade": _trade_summary(trade)})
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
