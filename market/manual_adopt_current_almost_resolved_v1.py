from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

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
    _save_state,
    _should_hold_to_resolution,
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


def _state_path() -> Path:
    return Path("logs") / "current_almost_resolved_manual_adopt_state.json"


def _tick_size_from_snap(snap: dict, side: str) -> float:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(side_book.get("tick_size"), 0.01))


def _token_id_for_side(snap: dict, side: str) -> str:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return str(side_book.get("token_id") or "")


def _order_snapshot(order: Any) -> dict:
    if order is None:
        return {}
    if hasattr(order, "as_dict"):
        try:
            return order.as_dict()
        except Exception:
            pass
    return {
        "order_id": getattr(order, "order_id", None),
        "token_id": getattr(order, "token_id", None),
        "side": getattr(order, "side", None),
        "price": getattr(order, "price", None),
        "original_size": getattr(order, "original_size", None),
        "size_matched": getattr(order, "size_matched", None),
        "status": getattr(order, "status", None),
        "outcome": getattr(order, "outcome", None),
        "market_slug": getattr(order, "market_slug", None),
        "order_type": getattr(order, "order_type", None),
    }


def _mention_order_id(record: Any, order_id: str) -> bool:
    if not order_id:
        return False
    try:
        return order_id in json.dumps(record, ensure_ascii=False, default=str)
    except Exception:
        return False


def _fill_qty_from_trade_history(broker, order_id: str) -> Optional[float]:
    if not order_id or not hasattr(broker, "get_trades"):
        return None
    try:
        trades = broker.get_trades()
    except Exception:
        return None
    best = None
    for trade in trades or []:
        if not isinstance(trade, dict):
            continue
        if not _mention_order_id(trade, order_id):
            continue
        for key in ("size_matched", "filled_size", "matched_size", "size", "qty", "quantity", "amount"):
            qty = _safe_float(trade.get(key), -1.0)
            if qty >= 0:
                best = max(best or 0.0, qty)
                break
    return best


def _trade_mentions_token(record: Any, token_id: str) -> bool:
    if not token_id:
        return False
    try:
        payload = json.dumps(record, ensure_ascii=False, default=str)
    except Exception:
        payload = str(record)
    return token_id in payload


def _avg_entry_from_trade_history(broker, *, token_id: str, market_slug: str) -> Optional[float]:
    if not token_id or not hasattr(broker, "get_trades"):
        return None
    try:
        trades = broker.get_trades()
    except Exception:
        return None
    notional = 0.0
    qty = 0.0
    for trade in trades or []:
        if not isinstance(trade, dict):
            continue
        side = str(trade.get("side") or trade.get("taker_side") or trade.get("maker_side") or "").upper()
        if side and side != "BUY":
            continue
        trade_market = str(trade.get("market_slug") or trade.get("market") or trade.get("slug") or "")
        if market_slug and trade_market and market_slug != trade_market:
            continue
        asset_id = str(trade.get("asset_id") or trade.get("token_id") or trade.get("asset") or "")
        if asset_id != token_id and not _trade_mentions_token(trade, token_id):
            continue
        trade_qty = 0.0
        for key in ("size", "size_matched", "matched_size", "filled_size", "qty", "quantity"):
            trade_qty = _safe_float(trade.get(key), 0.0)
            if trade_qty > 0:
                break
        price = _safe_float(trade.get("price"), 0.0)
        if trade_qty > 0 and price > 0:
            notional += trade_qty * price
            qty += trade_qty
    if qty <= 0:
        return None
    return round(notional / qty, 6)


def _manual_entry_candidates(broker, *, token_id: str, market_slug: str) -> list:
    if not token_id:
        return []
    try:
        open_orders = broker.get_open_orders(token_id=token_id)
    except Exception:
        open_orders = []
    candidates = []
    for order in open_orders or []:
        raw_side = str(getattr(order, "side", "") or "").upper()
        status = str(getattr(order, "status", "") or "").lower()
        if raw_side != "BUY":
            continue
        if status in ("canceled", "cancelled", "rejected", "closed", "resolved"):
            continue
        order_market_slug = str(getattr(order, "market_slug", "") or "")
        if order_market_slug and market_slug and order_market_slug != market_slug:
            continue
        candidates.append(order)
    return candidates


def _side_signal(signal: dict, *, side: str, event_slug: str) -> dict:
    out = dict(signal)
    out["side"] = side
    out["event_slug"] = event_slug
    return out


def _build_manual_trade_from_balance(
    *,
    signal: dict,
    snap: dict,
    side: str,
    token_id: str,
    qty: float,
    entry_price: float,
    now: float,
) -> LiveCurrentAlmostResolvedTradeState:
    tick_size = _tick_size_from_snap(snap, side)
    if entry_price <= 0:
        entry_price = _safe_float(signal.get("entry_price"), 0.0)
    if entry_price <= 0:
        side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
        entry_price = _safe_float(side_book.get("executable_sell") or side_book.get("best_bid"), 0.0)
    stop_price = _safe_float(signal.get("stop_price"), 0.0)
    if stop_price <= 0 and entry_price > 0:
        stop_price = round(max(0.01, entry_price - CurrentAlmostResolvedConfigV1().stop_ticks * tick_size), 6)
    return LiveCurrentAlmostResolvedTradeState(
        mode="open_position",
        event_slug=str(signal.get("event_slug") or ""),
        side=side,
        token_id=token_id,
        entry_order_id=None,
        exit_order_id=None,
        entry_price=entry_price if entry_price > 0 else None,
        entry_qty_requested=qty,
        entry_qty_filled=qty,
        exit_qty_filled=0.0,
        target_price=round(min(0.99, _safe_float(signal.get("exit_price"), 0.99)), 6),
        stop_price=round(max(0.01, stop_price), 6),
        best_bid=None,
        created_at=now,
        updated_at=now,
        hold_to_resolution=False,
        last_reason="manual_balance_adopted",
    )


def _build_manual_trade_from_order(
    *,
    signal: dict,
    snap: dict,
    order: Any,
    now: float,
) -> LiveCurrentAlmostResolvedTradeState:
    side = str(signal.get("side") or "").upper()
    tick_size = _tick_size_from_snap(snap, side)
    entry_price = _safe_float(getattr(order, "price", None), 0.0)
    requested_qty = _safe_float(getattr(order, "original_size", None), 0.0)
    matched_qty = _safe_float(getattr(order, "size_matched", None), 0.0)
    stop_price = _safe_float(signal.get("stop_price"), 0.0)
    if stop_price <= 0 and entry_price > 0:
        stop_price = round(max(0.01, entry_price - CurrentAlmostResolvedConfigV1().stop_ticks * tick_size), 6)
    trade = LiveCurrentAlmostResolvedTradeState(
        mode="manual_pending_entry" if matched_qty <= 0 else "open_position",
        event_slug=str(signal.get("event_slug") or ""),
        side=side,
        token_id=str(getattr(order, "token_id", "") or _token_id_for_side(snap, side)),
        entry_order_id=str(getattr(order, "order_id", "") or ""),
        exit_order_id=None,
        entry_price=entry_price if entry_price > 0 else None,
        entry_qty_requested=requested_qty,
        entry_qty_filled=matched_qty,
        exit_qty_filled=0.0,
        target_price=round(min(0.99, _safe_float(signal.get("exit_price"), 0.99)), 6),
        stop_price=round(max(0.01, stop_price), 6),
        created_at=now,
        updated_at=now,
        hold_to_resolution=False,
        last_reason="manual_order_linked",
    )
    if matched_qty > 0:
        trade.last_reason = "manual_fill_detected"
    return trade


def _sync_manual_trade_from_broker(broker, trade: LiveCurrentAlmostResolvedTradeState) -> tuple[LiveCurrentAlmostResolvedTradeState, Optional[Any], float, str]:
    entry_order = _get_order_status(broker, trade.entry_order_id)
    status = str(getattr(entry_order, "status", "") or "").lower() if entry_order is not None else ""
    matched = _safe_float(getattr(entry_order, "size_matched", None), 0.0) if entry_order is not None else 0.0

    if matched <= 0 and trade.entry_order_id:
        history_fill = _fill_qty_from_trade_history(broker, trade.entry_order_id)
        if history_fill is not None:
            matched = max(matched, history_fill)

    if matched > trade.entry_qty_filled:
        trade.entry_qty_filled = matched

    if entry_order is not None:
        trade.entry_price = _safe_float(getattr(entry_order, "price", None), _safe_float(trade.entry_price, 0.0))
        trade.entry_qty_requested = max(trade.entry_qty_requested, _safe_float(getattr(entry_order, "original_size", None), trade.entry_qty_requested))

    if trade.entry_qty_filled > 0:
        trade.mode = "open_position"
        trade.last_reason = "manual_fill_confirmed"

    return trade, entry_order, matched, status


def _manual_exit_reason(
    trade: LiveCurrentAlmostResolvedTradeState,
    *,
    bid_now: float,
    now: float,
    secs_to_end: Optional[int],
    signal: dict,
    cfg: CurrentAlmostResolvedConfigV1,
    flatten_deadline_secs: int,
    hold_winner_to_resolution: bool,
) -> Optional[str]:
    if bid_now <= 0:
        return None
    side = trade.side or "UP"
    trade.best_bid = max(_safe_float(trade.best_bid), bid_now)
    buffer_bps = _safe_float(signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"), 0.0)
    open_distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    market_range_15s = _safe_float(signal.get("market_range_15s"), 0.0)
    market_range_30s = _safe_float(signal.get("market_range_30s"), 0.0)
    adverse_spot_bps = _safe_float(signal.get("up_adverse_spot_bps" if side == "UP" else "down_adverse_spot_bps"), 0.0)
    edge_vs_counter = _safe_float(signal.get("up_edge_vs_counter" if side == "UP" else "down_edge_vs_counter"), 0.0)
    setup_variant = str(trade.setup_variant or signal.get("setup_variant") or "")
    pnl_positive = bid_now > _safe_float(trade.entry_price, 0.0)

    if not hold_winner_to_resolution and secs_to_end is not None and secs_to_end <= flatten_deadline_secs:
        return "deadline_flatten"
    if not hold_winner_to_resolution and not trade.hold_to_resolution and trade.target_price is not None and bid_now >= float(trade.target_price):
        return "target"
    if (
        setup_variant == "controlled_late_entry"
        and pnl_positive
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
    if trade.stop_price is not None and bid_now <= float(trade.stop_price):
        return "stop"
    if setup_variant == "extreme_99_limit":
        trade.hold_to_resolution = True
        return None
    if setup_variant == "resolved_pullback_limit" and secs_to_end is not None and secs_to_end <= cfg.resolved_pullback_preferred_secs:
        trade.hold_to_resolution = True
    if (
        adverse_spot_bps >= cfg.controlled_late_max_adverse_spot_15s_bps
        or market_range_30s >= cfg.paper_profit_take_on_market_range_30s
        or edge_vs_counter <= cfg.paper_structural_stop_edge_vs_counter
        or (signal.get("side") not in (None, side) and signal.get("allow"))
        or buffer_bps <= cfg.paper_structural_stop_buffer_bps
    ):
        return "profit_protect" if pnl_positive else "structural_stop"
    if not hold_winner_to_resolution and not trade.hold_to_resolution and now - trade.created_at >= cfg.max_hold_secs:
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
    adopt_existing_balance = _env_bool("POLY_MANUAL_ADOPT_EXISTING_BALANCE", True)
    hold_winner_to_resolution = _env_bool("POLY_MANUAL_ADOPT_HOLD_WINNER_TO_RESOLUTION", True)
    flatten_deadline_secs = _env_int("POLY_MANUAL_ADOPT_FLATTEN_DEADLINE_SECS", 2)
    min_limit_exit_qty = _env_float("POLY_MANUAL_ADOPT_MIN_LIMIT_EXIT_QTY", 5.0)
    poll_secs = max(0.25, _env_float("POLY_MANUAL_ADOPT_POLL_SECS", 0.5))
    run_for = int(duration_seconds or _env_int("POLY_MANUAL_ADOPT_RUN_SECONDS", 1800))
    session_dir = Path(log_dir) if log_dir else _build_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "manual_adopt_current_almost_resolved.jsonl"
    state_path = _state_path()

    broker = PolymarketBrokerV3.from_env()
    if not broker.healthcheck().ok:
        print("[GUARD] Broker healthcheck failed")
        return

    current_scalp = CurrentScalpResearchV1(cfg=scalp_cfg)
    trade = _load_state(state_path) or LiveCurrentAlmostResolvedTradeState()
    if trade.mode not in ("idle", "awaiting_redeem"):
        trade = _sync_manual_trade_from_broker(broker, trade)[0]
        if trade.mode == "idle":
            _clear_state(state_path)

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
            signal["event_slug"] = str(current_item.get("slug") or "")

        _append_jsonl(
            log_path,
            {
                "type": "snapshot",
                "ts": now,
                "current_secs": current_secs,
                "signal": signal,
                "trade": _trade_summary(trade),
            },
        )

        if not current_item or not current_snap:
            time.sleep(poll_secs)
            continue

        current_market_slug = str(current_item.get("slug") or "")
        side_tokens = {
            "UP": _token_id_for_side(current_snap, "UP"),
            "DOWN": _token_id_for_side(current_snap, "DOWN"),
        }
        current_token_id = _token_id_for_side(current_snap, str(signal.get("side") or "UP"))
        candidate_orders = _manual_entry_candidates(broker, token_id=current_token_id, market_slug=current_market_slug)
        buy_count = len(candidate_orders)

        if trade.mode == "idle":
            if adopt_existing_balance:
                balance_candidates = []
                for side_name, token_id in side_tokens.items():
                    if not token_id:
                        continue
                    token_balance_qty = _token_balance_qty(broker, token_id)
                    if token_balance_qty >= min_adopt_qty:
                        balance_candidates.append((side_name, token_id, token_balance_qty))
                if len(balance_candidates) == 1:
                    balance_side, balance_token_id, balance_qty = balance_candidates[0]
                    balance_signal = _side_signal(signal, side=balance_side, event_slug=current_market_slug)
                    avg_entry = _avg_entry_from_trade_history(
                        broker,
                        token_id=balance_token_id,
                        market_slug=current_market_slug,
                    )
                    trade = _build_manual_trade_from_balance(
                        signal=balance_signal,
                        snap=current_snap,
                        side=balance_side,
                        token_id=balance_token_id,
                        qty=balance_qty,
                        entry_price=_safe_float(avg_entry, 0.0),
                        now=now,
                    )
                    _save_state(state_path, trade)
                    print(
                        f"[MANUAL_ADOPT] adopted balance side={trade.side} entry={trade.entry_price} "
                        f"qty={trade.entry_qty_filled} stop={trade.stop_price}"
                    )
                    _append_jsonl(
                        log_path,
                        {
                            "type": "manual_balance_adopted",
                            "ts": now,
                            "balance_side": balance_side,
                            "token_balance_qty": balance_qty,
                            "avg_entry_from_history": avg_entry,
                            "trade": _trade_summary(trade),
                            "signal": balance_signal,
                        },
                    )
                elif len(balance_candidates) > 1:
                    print("[MANUAL_ADOPT] refusing: balances on both sides of current market")
                    _append_jsonl(
                        log_path,
                        {
                            "type": "manual_refused",
                            "ts": now,
                            "reason": "ambiguous_side_balances",
                            "balances": [
                                {"side": side_name, "token_id": token_id, "qty": qty}
                                for side_name, token_id, qty in balance_candidates
                            ],
                        },
                    )
                    time.sleep(poll_secs)
                    continue
            if trade.mode == "idle" and not current_token_id:
                time.sleep(poll_secs)
                continue

        if trade.mode == "idle":
            try:
                current_orders = broker.get_open_orders(token_id=current_token_id)
            except Exception:
                current_orders = []
            sell_conflicts = []
            for order in current_orders or []:
                if str(getattr(order, "side", "") or "").upper() == "SELL":
                    sell_conflicts.append(order)
            if sell_conflicts:
                print(f"[MANUAL_ADOPT] refusing: exit orders already open for {current_market_slug}")
                _append_jsonl(
                    log_path,
                    {
                        "type": "manual_refused",
                        "ts": now,
                        "reason": "exit_orders_open",
                        "market_slug": current_market_slug,
                        "sell_orders": [_order_snapshot(o) for o in sell_conflicts],
                    },
                )
                time.sleep(poll_secs)
                continue
            if buy_count == 0:
                time.sleep(poll_secs)
                continue
            if buy_count != 1:
                print(f"[MANUAL_ADOPT] refusing: expected exactly one manual BUY order, found {buy_count}")
                _append_jsonl(
                    log_path,
                    {
                        "type": "manual_refused",
                        "ts": now,
                        "reason": "ambiguous_buy_orders",
                        "market_slug": current_market_slug,
                        "buy_orders": [_order_snapshot(o) for o in candidate_orders],
                    },
                )
                time.sleep(poll_secs)
                continue
            if not signal.get("allow") or str(signal.get("side") or "") not in ("UP", "DOWN"):
                _append_jsonl(
                    log_path,
                    {
                        "type": "manual_refused",
                        "ts": now,
                        "reason": "signal_not_allowed",
                        "signal": signal,
                        "order": _order_snapshot(candidate_orders[0]),
                    },
                )
                time.sleep(poll_secs)
                continue

            order = candidate_orders[0]
            signal_side = str(signal.get("side") or "").upper()
            order_outcome = str(getattr(order, "outcome", "") or "").upper()
            if order_outcome and order_outcome != signal_side:
                print(
                    f"[MANUAL_ADOPT] refusing: order outcome {order_outcome} does not match signal side {signal_side}"
                )
                _append_jsonl(
                    log_path,
                    {
                        "type": "manual_refused",
                        "ts": now,
                        "reason": "side_mismatch",
                        "signal_side": signal_side,
                        "order": _order_snapshot(order),
                        "signal": signal,
                    },
                )
                time.sleep(poll_secs)
                continue

            order_qty = _safe_float(getattr(order, "original_size", None), 0.0)
            order_fill = _safe_float(getattr(order, "size_matched", None), 0.0)
            if max(order_qty, order_fill) < min_adopt_qty:
                _append_jsonl(
                    log_path,
                    {
                        "type": "manual_refused",
                        "ts": now,
                        "reason": "order_too_small",
                        "min_adopt_qty": min_adopt_qty,
                        "order": _order_snapshot(order),
                    },
                )
                time.sleep(poll_secs)
                continue

            trade = _build_manual_trade_from_order(signal=signal, snap=current_snap, order=order, now=now)
            if trade.entry_qty_filled <= 0:
                trade.mode = "manual_pending_entry"
            else:
                trade.mode = "open_position"
            _save_state(state_path, trade)
            print(
                f"[MANUAL_ADOPT] linked order_id={trade.entry_order_id} side={trade.side} price={trade.entry_price} "
                f"qty={trade.entry_qty_requested} matched={trade.entry_qty_filled} mode={trade.mode}"
            )
            _append_jsonl(
                log_path,
                {
                    "type": "manual_linked",
                    "ts": now,
                    "trade": _trade_summary(trade),
                    "signal": signal,
                    "order": _order_snapshot(order),
                },
            )

        if trade.mode in ("manual_pending_entry", "open_position", "pending_exit", "exit_pending_confirm"):
            trade, entry_order, matched_qty, status = _sync_manual_trade_from_broker(broker, trade)
            if entry_order is not None:
                _save_state(state_path, trade)
                if matched_qty > 0:
                    _append_jsonl(
                        log_path,
                        {
                            "type": "manual_fill_sync",
                            "ts": now,
                            "status": status,
                            "matched_qty": matched_qty,
                            "trade": _trade_summary(trade),
                            "order": _order_snapshot(entry_order),
                        },
                    )
            else:
                if trade.entry_qty_filled <= 0:
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)
                    _append_jsonl(
                        log_path,
                        {
                            "type": "manual_lost",
                            "ts": now,
                            "reason": "entry_order_missing_without_verified_fill",
                        },
                    )
                    time.sleep(poll_secs)
                    continue

        active_book = _fetch_active_book(trade) if trade.mode in ("manual_pending_entry", "open_position", "pending_exit", "exit_pending_confirm") else None
        active_bid = _best_bid(active_book or {})
        if trade.side and current_snap:
            exec_bid = _safe_float(current_exec.get("up_bid" if trade.side == "UP" else "down_bid"), 0.0) if current_exec else 0.0
            if exec_bid > 0:
                active_bid = max(active_bid, exec_bid)

        if trade.mode == "manual_pending_entry":
            if trade.entry_qty_filled > 0:
                trade.mode = "open_position"
                trade.last_reason = "manual_fill_detected"
                _save_state(state_path, trade)
                _append_jsonl(log_path, {"type": "manual_opened", "ts": now, "trade": _trade_summary(trade)})
            else:
                time.sleep(poll_secs)
                continue

        if trade.mode == "open_position":
            if trade.side and _should_hold_to_resolution(
                signal, bid_now=active_bid, secs_to_end=current_secs, cfg=signal_cfg, side=trade.side
            ):
                trade.hold_to_resolution = True
            if hold_winner_to_resolution and current_secs is not None and current_secs <= 1:
                trade.mode = "awaiting_redeem"
                trade.updated_at = now
                trade.last_reason = "manual_resolution_awaiting_redeem"
                _save_state(state_path, trade)
                _append_jsonl(
                    log_path,
                    {
                        "type": "awaiting_redeem",
                        "ts": now,
                        "reason": "manual_resolution_awaiting_redeem",
                        "active_bid": active_bid,
                        "trade": _trade_summary(trade),
                        "signal": signal,
                    },
                )
                time.sleep(poll_secs)
                continue
            reason = _manual_exit_reason(
                trade,
                bid_now=active_bid,
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
                _append_jsonl(log_path, {"type": "exit_posted", "ts": now, "reason": reason, "trade": _trade_summary(trade), "signal": signal})

        if trade.mode == "pending_exit":
            token_balance_qty = _token_balance_qty(broker, trade.token_id)
            exit_order = _get_order_status(broker, trade.exit_order_id)
            if exit_order is None:
                if _is_flat_qty(token_balance_qty):
                    _append_jsonl(log_path, {"type": "flat", "ts": now, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)
                elif now - trade.updated_at >= 1.0:
                    trade = _post_exit_order(
                        broker,
                        trade,
                        exit_price=active_bid if active_bid > 0 else 0.01,
                        now=now,
                        reason="exit_repost",
                        min_limit_exit_qty=min_limit_exit_qty,
                    )
                    _save_state(state_path, trade)
                    _append_jsonl(log_path, {"type": "exit_repost", "ts": now, "trade": _trade_summary(trade)})
            else:
                trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
                status = str(getattr(exit_order, "status", "") or "").lower()
                if _is_flat_qty(token_balance_qty) or trade.remaining_position_qty <= 0:
                    _append_jsonl(log_path, {"type": "flat", "ts": now, "exit_order": _order_snapshot(exit_order), "trade": _trade_summary(trade)})
                    trade = LiveCurrentAlmostResolvedTradeState()
                    _clear_state(state_path)
                elif status in ("matched", "filled", "closed", "resolved") and token_balance_qty > 0:
                    trade.mode = "exit_pending_confirm"
                    trade.last_reason = f"exit_match_pending_confirm:{round(token_balance_qty, 6)}"
                    trade.confirm_started_at = now
                    trade.confirm_polls = 1
                    _save_state(state_path, trade)
                    _append_jsonl(log_path, {"type": "exit_match_pending_confirm", "ts": now, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                elif now - trade.updated_at >= 1.0:
                    cancel_resp = broker.cancel_order(trade.exit_order_id)
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
                    _append_jsonl(log_path, {"type": "exit_repost", "ts": now, "cancel": cancel_resp, "trade": _trade_summary(trade)})

        if trade.mode == "exit_pending_confirm":
            token_balance_qty = _token_balance_qty(broker, trade.token_id)
            if _is_flat_qty(token_balance_qty):
                _append_jsonl(log_path, {"type": "flat", "ts": now, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
                trade = LiveCurrentAlmostResolvedTradeState()
                _clear_state(state_path)
            elif now - trade.confirm_started_at >= 2.0 or trade.confirm_polls >= 2:
                trade.mode = "pending_exit"
                trade.exit_order_id = None
                trade.updated_at = now - 1.0
                trade.last_reason = f"residual_position_after_exit:{round(token_balance_qty, 6)}"
                _save_state(state_path, trade)
                _append_jsonl(log_path, {"type": "residual_position_after_exit", "ts": now, "token_balance_qty": token_balance_qty, "trade": _trade_summary(trade)})
            else:
                trade.confirm_polls += 1
                _save_state(state_path, trade)

        if trade.mode == "awaiting_redeem":
            token_balance_qty = _token_balance_qty(broker, trade.token_id)
            _append_jsonl(
                log_path,
                {
                    "type": "awaiting_redeem_status",
                    "ts": now,
                    "token_balance_qty": token_balance_qty,
                    "trade": _trade_summary(trade),
                },
            )
            if _is_flat_qty(token_balance_qty):
                trade = LiveCurrentAlmostResolvedTradeState()
                _clear_state(state_path)
                _append_jsonl(log_path, {"type": "flat_after_redeem", "ts": now, "token_balance_qty": token_balance_qty})

        time.sleep(poll_secs)


if __name__ == "__main__":
    monitor_manual_adopt_current_almost_resolved_v1()
