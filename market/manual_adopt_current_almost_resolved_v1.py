from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from market.broker_env import load_broker_env
from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.live_current_almost_resolved_real_v1 import (
    LiveCurrentAlmostResolvedTradeState,
    _append_jsonl,
    _best_bid,
    _clear_state,
    _fetch_active_book,
    _get_order_status,
    _is_flat_qty,
    _load_state,
    _post_exit_order,
    _restore_trade_from_broker,
    _save_state,
    _state_path,
    _token_balance_qty,
    _trade_summary,
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


def _build_log_dir() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"manual_adopt_current_almost_resolved_{ts}"


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


def _adopt_trade(
    *,
    signal: dict,
    snap: dict,
    side: str,
    qty: float,
    entry_price_hint: Optional[float],
    now: float,
) -> LiveCurrentAlmostResolvedTradeState:
    tick_size = _tick_size_from_snap(snap, side)
    signal_entry = _safe_float(signal.get("entry_price"), 0.0)
    entry_price = float(entry_price_hint) if entry_price_hint and entry_price_hint > 0 else signal_entry
    stop_price = _safe_float(signal.get("stop_price"), 0.0)
    if stop_price <= 0 and entry_price > 0:
        stop_price = round(max(0.01, entry_price - CurrentAlmostResolvedConfigV1().stop_ticks * tick_size), 6)
    return LiveCurrentAlmostResolvedTradeState(
        mode="open_position",
        event_slug=str(signal.get("event_slug") or ""),
        side=side,
        token_id=_token_id_for_side(snap, side),
        entry_order_id=None,
        exit_order_id=None,
        entry_price=entry_price if entry_price > 0 else None,
        entry_qty_requested=float(qty),
        entry_qty_filled=float(qty),
        exit_qty_filled=0.0,
        target_price=round(min(0.99, _safe_float(signal.get("exit_price"), 0.99)), 6),
        stop_price=round(max(0.01, stop_price), 6),
        created_at=now,
        updated_at=now,
        hold_to_resolution=False,
        last_reason="manual_adopted",
    )


def _manual_exit_reason(
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    bid_now: float,
    now: float,
    secs_to_end: Optional[int],
    signal: dict,
    cfg: CurrentAlmostResolvedConfigV1,
    flatten_deadline_secs: int,
) -> Optional[str]:
    if bid_now <= 0:
        return None
    side = trade.side or "UP"
    trade.best_bid = max(_safe_float(trade.best_bid), bid_now)
    buffer_bps = _safe_float(signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"), 0.0)
    market_range_30s = _safe_float(signal.get("market_range_30s"), 0.0)
    adverse_spot_bps = _safe_float(signal.get("up_adverse_spot_bps" if side == "UP" else "down_adverse_spot_bps"), 0.0)
    edge_vs_counter = _safe_float(signal.get("up_edge_vs_counter" if side == "UP" else "down_edge_vs_counter"), 0.0)

    if secs_to_end is not None and secs_to_end <= flatten_deadline_secs:
        return "deadline_flatten"
    if trade.stop_price is not None and bid_now <= float(trade.stop_price):
        return "stop"
    if signal.get("setup_variant") == "resolved_pullback_limit" and secs_to_end is not None and secs_to_end <= cfg.resolved_pullback_preferred_secs:
        trade.hold_to_resolution = True
    if (
        adverse_spot_bps >= cfg.controlled_late_max_adverse_spot_15s_bps
        or market_range_30s >= cfg.paper_profit_take_on_market_range_30s
        or edge_vs_counter <= cfg.paper_structural_stop_edge_vs_counter
        or (signal.get("side") not in (None, side) and signal.get("allow"))
        or buffer_bps <= cfg.paper_structural_stop_buffer_bps
    ):
        return "profit_protect" if bid_now > _safe_float(trade.entry_price, 0.0) else "structural_stop"
    if not trade.hold_to_resolution and trade.target_price is not None and bid_now >= float(trade.target_price):
        return "target"
    if not trade.hold_to_resolution and now - trade.created_at >= cfg.max_hold_secs:
        return "timeout"
    return None


def monitor_manual_adopt_current_almost_resolved_v1(duration_seconds: Optional[int] = None, log_dir: Optional[str] = None) -> None:
    load_dotenv()
    guarded_cfg = load_live_guarded_config()
    broker_status = load_broker_env()
    signal_cfg = CurrentAlmostResolvedConfigV1()
    scalp_cfg = CurrentScalpConfigV1()

    if not guarded_cfg.enabled or guarded_cfg.shadow_only or not guarded_cfg.real_posts_enabled:
        print("[GUARD] requires guarded real mode enabled")
        return
    if not _env_bool("POLY_MANUAL_ADOPT_CURRENT_ALMOST_RESOLVED_ENABLED", False):
        print("[GUARD] Set POLY_MANUAL_ADOPT_CURRENT_ALMOST_RESOLVED_ENABLED=true")
        return
    if not broker_status.ready_for_real_smoke:
        print("[GUARD] Broker env missing required credentials")
        return

    min_adopt_qty = _env_float("POLY_MANUAL_ADOPT_MIN_QTY", 1.0)
    flatten_deadline_secs = _env_int("POLY_MANUAL_ADOPT_FLATTEN_DEADLINE_SECS", 2)
    min_limit_exit_qty = _env_float("POLY_MANUAL_ADOPT_MIN_LIMIT_EXIT_QTY", 5.0)
    poll_secs = max(0.25, _env_float("POLY_MANUAL_ADOPT_POLL_SECS", 0.5))
    run_for = int(duration_seconds or _env_int("POLY_MANUAL_ADOPT_RUN_SECONDS", 1800))
    entry_price_hint = _env_float("POLY_MANUAL_ADOPT_ENTRY_PRICE", 0.0)
    session_dir = Path(log_dir) if log_dir else _build_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "manual_adopt_current_almost_resolved.jsonl"
    state_path = _state_path()

    broker = PolymarketBrokerV3.from_env()
    if not broker.healthcheck().ok:
        print("[GUARD] Broker healthcheck failed")
        return

    startup_orders = broker.get_open_orders()[:50]
    if startup_orders:
        print("[GUARD] Refusing to start with open orders already live")
        return

    current_scalp = CurrentScalpResearchV1(cfg=scalp_cfg)
    trade = _load_state(state_path) or LiveCurrentAlmostResolvedTradeState()
    if trade.mode != "idle":
        trade = _restore_trade_from_broker(broker, trade)

    current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}
    started_at = time.time()

    while time.time() - started_at < run_for:
        now = time.time()
        slot_bundle = _build_slot_bundle()
        current_item = slot_bundle["queue"].get("current")
        current_secs = int(current_item.get("seconds_to_end")) if current_item and current_item.get("seconds_to_end") is not None else None
        slot_state = _fetch_slot_state(slot_bundle)
        current_snap = _slot_snapshot(slot_state, "current")
        current_exec, _ = _compute_executable_metrics(current_snap)

        if current_item and current_item.get("slug") != current_open_reference.get("slug"):
            raw_event = fetch_event_by_slug(str(current_item.get("slug") or ""))
            market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
            event_start_time = market.get("eventStartTime") or raw_event.get("startTime") if raw_event else None
            open_ref = fetch_binance_open_price_for_event_start_v1(event_start_time) if event_start_time else {"open_price": None}
            current_open_reference = {"slug": current_item.get("slug"), "price": open_ref.get("open_price"), "event_start_time": event_start_time}

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
            signal["event_slug"] = str(current_item.get("slug") or "")

        up_qty = _token_balance_qty(broker, _token_id_for_side(current_snap, "UP"))
        down_qty = _token_balance_qty(broker, _token_id_for_side(current_snap, "DOWN"))
        _append_jsonl(
            log_path,
            {
                "type": "snapshot",
                "ts": now,
                "current_secs": current_secs,
                "signal": signal,
                "trade": _trade_summary(trade),
                "balances": {"up_qty": up_qty, "down_qty": down_qty},
            },
        )

        if trade.mode == "idle" and signal.get("allow") and signal.get("side") in ("UP", "DOWN"):
            desired_side = str(signal.get("side"))
            qty = up_qty if desired_side == "UP" else down_qty
            other_qty = down_qty if desired_side == "UP" else up_qty
            if qty >= min_adopt_qty and other_qty < min_adopt_qty * 0.5:
                trade = _adopt_trade(
                    signal=signal,
                    snap=current_snap,
                    side=desired_side,
                    qty=qty,
                    entry_price_hint=entry_price_hint if entry_price_hint > 0 else None,
                    now=now,
                )
                _save_state(state_path, trade)
                _append_jsonl(log_path, {"type": "manual_adopted", "ts": now, "trade": _trade_summary(trade), "signal": signal})

        if trade.mode in ("open_position", "pending_exit"):
            if trade.mode == "pending_exit":
                exit_order = _get_order_status(broker, trade.exit_order_id)
                balance_qty = _token_balance_qty(broker, trade.token_id)
                status = str(getattr(exit_order, "status", "") or "").lower() if exit_order else ""
                if _is_flat_qty(balance_qty) or status in ("filled", "closed", "resolved"):
                    _append_jsonl(log_path, {"type": "flat", "ts": now, "trade": _trade_summary(trade)})
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)
                else:
                    active_book = _fetch_active_book(trade)
                    active_bid = _best_bid(active_book or {})
                    if now - trade.updated_at >= 1.0 and active_bid > 0:
                        trade = _post_exit_order(broker, trade, exit_price=active_bid, now=now, reason="exit_repost", min_limit_exit_qty=min_limit_exit_qty)
                        _save_state(state_path, trade)
                        _append_jsonl(log_path, {"type": "exit_repost", "ts": now, "trade": _trade_summary(trade)})
            elif trade.mode == "open_position":
                bid_now = _bid_for_side(current_exec, trade.side or "UP")
                reason = _manual_exit_reason(
                    trade,
                    bid_now=bid_now,
                    now=now,
                    secs_to_end=current_secs,
                    signal=signal,
                    cfg=signal_cfg,
                    flatten_deadline_secs=flatten_deadline_secs,
                )
                if reason:
                    trade = _post_exit_order(broker, trade, exit_price=bid_now, now=now, reason=reason, min_limit_exit_qty=min_limit_exit_qty)
                    _save_state(state_path, trade)
                    _append_jsonl(log_path, {"type": "exit_posted", "ts": now, "reason": reason, "trade": _trade_summary(trade), "signal": signal})

        time.sleep(poll_secs)


if __name__ == "__main__":
    monitor_manual_adopt_current_almost_resolved_v1()
