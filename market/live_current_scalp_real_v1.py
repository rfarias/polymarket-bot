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
    return Path("logs") / f"current_scalp_real_{ts}"


def _state_path() -> Path:
    return Path("logs") / "current_scalp_real_state.json"


def _save_state(path: Path, trade) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(trade), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state(path: Path):
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return LiveCurrentScalpTradeState(**payload)
    except Exception:
        return None


def _clear_state(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


@dataclass
class LiveCurrentScalpTradeState:
    mode: str = "idle"  # idle | pending_entry | open_position | pending_exit
    event_slug: Optional[str] = None
    side: Optional[str] = None
    token_id: Optional[str] = None
    setup: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    entry_price: Optional[float] = None
    entry_qty_requested: float = 0.0
    entry_qty_filled: float = 0.0
    exit_qty_filled: float = 0.0
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    best_bid: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_reason: Optional[str] = None

    @property
    def remaining_position_qty(self) -> float:
        return round(max(0.0, float(self.entry_qty_filled) - float(self.exit_qty_filled)), 6)


def _tick_size_from_snap(snap: dict, side: str) -> float:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(side_book.get("tick_size"), 0.01))


def _token_id_for_side(snap: dict, side: str) -> str:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return str(side_book.get("token_id") or "")


def _fetch_active_book(trade: LiveCurrentScalpTradeState) -> Optional[dict]:
    if not trade.token_id:
        return None
    raw_books = fetch_books_for_tokens([trade.token_id])
    if not raw_books:
        return None
    return raw_books[0] if raw_books else None


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


def _has_sufficient_collateral_for_entry(broker, *, entry_price: float, qty: float, buffer_usd: float = 0.25) -> bool:
    required = round(float(entry_price) * float(qty) + float(buffer_usd), 6)
    available = _collateral_balance_usd(broker)
    return available >= required


def _restore_trade_from_broker(broker, trade: LiveCurrentScalpTradeState) -> LiveCurrentScalpTradeState:
    if trade.mode == "idle":
        return trade
    entry_order = _get_order_status(broker, trade.entry_order_id)
    if entry_order is not None:
        trade.entry_qty_filled = max(trade.entry_qty_filled, _safe_float(getattr(entry_order, "size_matched", None), 0.0))
    exit_order = _get_order_status(broker, trade.exit_order_id)
    if exit_order is not None:
        trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
        status = str(getattr(exit_order, "status", "") or "").lower()
        if trade.remaining_position_qty <= 0 or status in ("filled", "closed", "resolved"):
            return LiveCurrentScalpTradeState()
        trade.mode = "pending_exit"
    if trade.entry_qty_filled > 0 and trade.mode == "pending_entry":
        trade.mode = "open_position"
    token_balance = _token_balance_qty(broker, trade.token_id)
    if token_balance > 0 and trade.entry_qty_filled > 0:
        trade.entry_qty_filled = max(trade.entry_qty_filled, token_balance + float(trade.exit_qty_filled))
    return trade


def _post_entry_order(
    broker,
    *,
    signal: dict,
    snap: dict,
    qty: int,
    tick_size: float,
    now: float,
    cfg: CurrentScalpConfigV1,
) -> LiveCurrentScalpTradeState:
    side = str(signal.get("side") or "")
    entry_price = _safe_float(signal.get("entry_price"), 0.0)
    trade = LiveCurrentScalpTradeState(
        mode="pending_entry",
        event_slug=str(signal.get("event_slug") or ""),
        side=side,
        token_id=_token_id_for_side(snap, side),
        setup=str(signal.get("setup") or ""),
        entry_price=entry_price,
        entry_qty_requested=float(qty),
        target_price=round(min(0.99, entry_price + cfg.target_ticks * tick_size), 6),
        stop_price=round(max(0.01, entry_price - cfg.stop_ticks * tick_size), 6),
        created_at=now,
        updated_at=now,
        last_reason="entry_posted",
    )
    if not trade.token_id:
        raise RuntimeError(f"Missing token_id for side={side}")
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
        client_order_key=f"current_scalp:entry:{int(now)}:{side}",
    )
    order = broker.place_limit_order(req)
    trade.entry_order_id = order.order_id
    return trade


def _post_exit_order(
    broker,
    trade: LiveCurrentScalpTradeState,
    *,
    exit_price: float,
    now: float,
    reason: str,
) -> LiveCurrentScalpTradeState:
    token_balance_qty = _token_balance_qty(broker, trade.token_id)
    qty = min(trade.remaining_position_qty, token_balance_qty) if token_balance_qty > 0 else trade.remaining_position_qty
    if qty <= 0:
        trade.mode = "idle"
        trade.last_reason = "flat"
        trade.updated_at = now
        return trade
    try:
        broker.update_balance_allowance(asset_type="CONDITIONAL", token_id=trade.token_id)
    except Exception:
        pass
    req = BrokerOrderRequest(
        token_id=trade.token_id or "",
        side="SELL",
        price=float(exit_price),
        size=float(qty),
        market_slug=trade.event_slug,
        outcome=trade.side,
        client_order_key=f"current_scalp:exit:{reason}:{int(now)}:{trade.side}",
    )
    order = broker.place_limit_order(req)
    trade.exit_order_id = order.order_id
    trade.mode = "pending_exit"
    trade.updated_at = now
    trade.last_reason = f"exit_posted:{reason}"
    return trade


def _force_risk_cleanup(broker, trade: LiveCurrentScalpTradeState, log_path: Path, now: float, reason: str) -> None:
    try:
        _append_jsonl(log_path, {"type": "panic", "ts": now, "reason": reason, "trade": asdict(trade)})
        _cancel_if_live(broker, trade.entry_order_id)
        _cancel_if_live(broker, trade.exit_order_id)
        token_balance_qty = _token_balance_qty(broker, trade.token_id)
        panic_qty = min(trade.remaining_position_qty, token_balance_qty) if token_balance_qty > 0 else trade.remaining_position_qty
        if panic_qty > 0 and trade.token_id and trade.side:
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
                    market_slug=trade.event_slug,
                    outcome=trade.side,
                    client_order_key=f"current_scalp:panic_exit:{int(now)}:{trade.side}",
                )
                order = broker.place_limit_order(req)
                _append_jsonl(log_path, {"type": "panic_exit_posted", "ts": now, "price": panic_bid, "order": order.as_dict()})
    except Exception as exc:
        _append_jsonl(log_path, {"type": "panic_error", "ts": now, "reason": reason, "error": f"{type(exc).__name__}: {exc}"})


def monitor_live_current_scalp_real_v1(duration_seconds: Optional[int] = None, log_dir: Optional[str] = None) -> None:
    load_dotenv()
    guarded_cfg = load_live_guarded_config()
    broker_status = load_broker_env()
    cfg = CurrentScalpConfigV1()

    print("[BROKER_ENV]", broker_status.as_dict())
    print("[LIVE_GUARDED_CONFIG]", guarded_cfg.as_dict())
    print("[CURRENT_SCALP_CONFIG]", cfg.as_dict())

    if not guarded_cfg.enabled:
        print("[GUARD] Set POLY_GUARDED_ENABLED=true")
        return
    if guarded_cfg.shadow_only:
        print("[GUARD] Set POLY_GUARDED_SHADOW_ONLY=false")
        return
    if not guarded_cfg.real_posts_enabled:
        print("[GUARD] Set POLY_GUARDED_REAL_POSTS_ENABLED=true")
        return
    if not _env_bool("POLY_CURRENT_SCALP_REAL_ENABLED", False):
        print("[GUARD] Set POLY_CURRENT_SCALP_REAL_ENABLED=true to arm real current scalp")
        return
    if not broker_status.ready_for_real_smoke:
        print("[GUARD] Broker env missing required credentials")
        return

    qty = _env_int("POLY_CURRENT_SCALP_QTY", 1)
    entry_timeout_secs = _env_int("POLY_CURRENT_SCALP_ENTRY_TIMEOUT_SECS", 12)
    position_timeout_secs = _env_int("POLY_CURRENT_SCALP_POSITION_TIMEOUT_SECS", cfg.max_hold_secs)
    poll_secs = max(0.5, float(os.getenv("POLY_CURRENT_SCALP_POLL_SECS", "1.0")))
    run_for = int(duration_seconds or _env_int("POLY_CURRENT_SCALP_RUN_SECONDS", 1800))
    session_dir = Path(log_dir) if log_dir else _build_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "current_scalp_real.jsonl"
    exception_path = session_dir / "exception.log"
    state_path = _state_path()

    print(
        "[CURRENT_SCALP_REAL_PARAMS]",
        {
            "qty": qty,
            "entry_timeout_secs": entry_timeout_secs,
            "position_timeout_secs": position_timeout_secs,
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
    restored_trade = _load_state(state_path) or LiveCurrentScalpTradeState()

    if restored_trade.mode != "idle":
        restored_trade = _restore_trade_from_broker(broker, restored_trade)
        print("[RESTORED_CURRENT_SCALP_TRADE]", asdict(restored_trade))
        if restored_trade.mode == "idle":
            _clear_state(state_path)
        else:
            allowed_ids = {x for x in (restored_trade.entry_order_id, restored_trade.exit_order_id) if x}
            startup_ids = {o.order_id for o in startup_orders}
            if startup_ids - allowed_ids:
                print("[GUARD] Refusing to start with open orders not owned by restored current scalp state.")
                return
    elif startup_orders:
        print("[GUARD] Refusing to start with open orders while no current scalp state is restored.")
        return

    research = CurrentScalpResearchV1(cfg=cfg)
    trade = restored_trade
    current_open_reference: dict = {"slug": None, "price": None, "event_start_time": None}
    started_at = time.time()

    while time.time() - started_at < run_for:
        now = time.time()
        try:
            slot_bundle = _build_slot_bundle()
            current_item = slot_bundle["queue"].get("current")
            current_secs = int(current_item.get("seconds_to_end")) if current_item and current_item.get("seconds_to_end") is not None else None
            slot_state = _fetch_slot_state(slot_bundle)
            current_snap = _slot_snapshot(slot_state, "current")

            if current_item and current_item["slug"] != current_open_reference.get("slug"):
                raw_event = fetch_event_by_slug(current_item["slug"])
                market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
                start_time = market.get("eventStartTime") or raw_event.get("startTime") if raw_event else None
                opened = fetch_binance_open_price_for_event_start_v1(start_time) if start_time else {"open_price": None}
                current_open_reference = {"slug": current_item["slug"], "price": opened.get("open_price"), "event_start_time": start_time}

            reference = fetch_external_btc_reference_v1() if current_item else {}
            signal = (
                research.evaluate(
                    snap=current_snap,
                    secs_to_end=current_secs,
                    event_start_time=current_open_reference.get("event_start_time"),
                    now_ts=now,
                    reference_price=reference.get("reference_price"),
                    source_divergence_bps=reference.get("source_divergence_bps"),
                    opening_reference_price=current_open_reference.get("price"),
                )
                if current_item
                else {"setup": "no_edge", "allow": False}
            )
            if current_item:
                signal["event_slug"] = current_item["slug"]

            executable, executable_reason = _compute_executable_metrics(current_snap)
            active_book = _fetch_active_book(trade) if trade.mode in ("pending_entry", "open_position", "pending_exit") else None
            active_bid = _best_bid(active_book or {})
            if trade.side and executable:
                exec_bid = _safe_float(executable.get("up_bid" if trade.side == "UP" else "down_bid"), 0.0)
                if exec_bid > 0:
                    active_bid = max(active_bid, exec_bid)

            snapshot = {
                "type": "snapshot",
                "ts": now,
                "current_slug": current_item.get("slug") if current_item else None,
                "current_secs": current_secs,
                "executable_reason": executable_reason,
                "signal": signal,
                "trade": asdict(trade),
                "active_bid": active_bid,
            }
            _append_jsonl(log_path, snapshot)
            print(
                f"[CURRENT_SCALP_REAL] current_secs={current_secs} setup={signal.get('setup')} "
                f"allow={signal.get('allow')} side={signal.get('side')} mode={trade.mode} qty={trade.entry_qty_filled}/{trade.remaining_position_qty}"
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
                    cfg=cfg,
                )
                _save_state(state_path, trade)
                _append_jsonl(log_path, {"type": "enter", "ts": now, "signal": signal, "trade": asdict(trade)})
                time.sleep(poll_secs)
                continue

            if trade.mode in ("pending_entry", "open_position", "pending_exit"):
                entry_order = _get_order_status(broker, trade.entry_order_id)
                if entry_order is not None:
                    trade.entry_qty_filled = max(trade.entry_qty_filled, _safe_float(getattr(entry_order, "size_matched", None), 0.0))
                token_balance = _token_balance_qty(broker, trade.token_id)
                if token_balance > 0:
                    trade.entry_qty_filled = max(trade.entry_qty_filled, token_balance + float(trade.exit_qty_filled))
                trade.updated_at = now
                _save_state(state_path, trade)

            if trade.mode == "pending_entry":
                if trade.entry_qty_filled > 0:
                    order = _get_order_status(broker, trade.entry_order_id)
                    if order and getattr(order, "remaining_size", 0.0) > 0:
                        resp = _cancel_if_live(broker, trade.entry_order_id)
                        _append_jsonl(log_path, {"type": "entry_cancel_remainder", "ts": now, "response": resp})
                    trade.mode = "open_position"
                    trade.updated_at = now
                    trade.last_reason = "entry_fill_detected"
                    _save_state(state_path, trade)
                    _append_jsonl(log_path, {"type": "fill", "ts": now, "trade": asdict(trade)})
                elif now - trade.created_at >= entry_timeout_secs:
                    resp = _cancel_if_live(broker, trade.entry_order_id)
                    _append_jsonl(log_path, {"type": "entry_cancel_timeout", "ts": now, "response": resp, "trade": asdict(trade)})
                    trade = LiveCurrentScalpTradeState()
                    _clear_state(state_path)

            if trade.mode == "open_position":
                trade.best_bid = max(_safe_float(trade.best_bid, 0.0), active_bid)
                if active_bid > 0:
                    if active_bid >= _safe_float(trade.target_price):
                        trade = _post_exit_order(broker, trade, exit_price=active_bid, now=now, reason="target")
                    elif active_bid <= _safe_float(trade.stop_price):
                        trade = _post_exit_order(broker, trade, exit_price=active_bid, now=now, reason="stop")
                    elif current_secs is not None and current_secs <= 8:
                        trade = _post_exit_order(broker, trade, exit_price=active_bid, now=now, reason="expiry_near")
                    elif now - trade.updated_at >= position_timeout_secs:
                        trade = _post_exit_order(broker, trade, exit_price=active_bid, now=now, reason="timeout")
                    if trade.mode == "pending_exit":
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "exit_posted", "ts": now, "trade": asdict(trade)})

            if trade.mode == "pending_exit":
                exit_order = _get_order_status(broker, trade.exit_order_id)
                if exit_order is not None:
                    trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
                    status = str(getattr(exit_order, "status", "") or "").lower()
                    if trade.remaining_position_qty <= 0 or status in ("filled", "closed", "resolved"):
                        _append_jsonl(log_path, {"type": "flat", "ts": now, "trade": asdict(trade)})
                        trade = LiveCurrentScalpTradeState()
                        _clear_state(state_path)
                    elif now - trade.updated_at >= position_timeout_secs:
                        cancel_resp = _cancel_if_live(broker, trade.exit_order_id)
                        _append_jsonl(log_path, {"type": "exit_cancel_timeout", "ts": now, "cancel": cancel_resp, "trade": asdict(trade)})
                        trade = LiveCurrentScalpTradeState()
                        _clear_state(state_path)

            time.sleep(poll_secs)

        except Exception as exc:
            trace = traceback.format_exc()
            exception_path.write_text(trace, encoding="utf-8")
            _append_jsonl(
                log_path,
                {
                    "type": "exception",
                    "ts": now,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": trace,
                    "trade": asdict(trade),
                },
            )
            _force_risk_cleanup(broker, trade, log_path, now, f"{type(exc).__name__}: {exc}")
            _save_state(state_path, trade)
            raise


if __name__ == "__main__":
    monitor_live_current_scalp_real_v1()
