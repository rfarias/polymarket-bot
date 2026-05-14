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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_log_dir() -> Path:
    return Path("logs") / f"counter_reversal_real_{time.strftime('%Y%m%d_%H%M%S')}"


def _state_path() -> Path:
    return Path("logs") / "counter_reversal_real_state.json"


def _current_almost_resolved_state_path() -> Path:
    return Path("logs") / "current_almost_resolved_real_state.json"


def _state_file_active(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return str(payload.get("mode") or "idle") != "idle"


@dataclass
class CounterReversalConfigV1:
    min_secs_to_end: int = 45
    max_secs_to_end: int = 75
    min_leader_price: float = 0.95
    max_leader_price: float = 0.965
    max_counter_price: float = 0.04
    min_counter_price: float = 0.01
    min_adverse_5s_bps: float = 1.0
    max_abs_distance_bps: float = 7.0
    max_distance_vs_range_60s: float = 2.5
    max_source_divergence_bps: float = 8.0
    max_spread: float = 0.03
    min_depth_top3: float = 10.0
    qty: float = 5.0
    entry_order_type: str = "FAK"
    entry_timeout_secs: float = 1.5
    target_price: float = 0.18
    runner_price: float = 0.45
    stop_price: float = 0.01
    flatten_deadline_secs: int = 4
    exit_repost_secs: float = 0.75
    poll_secs: float = 0.5

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_env(cls) -> "CounterReversalConfigV1":
        return cls(
            min_secs_to_end=_env_int("POLY_COUNTER_REVERSAL_MIN_SECS", cls.min_secs_to_end),
            max_secs_to_end=_env_int("POLY_COUNTER_REVERSAL_MAX_SECS", cls.max_secs_to_end),
            min_leader_price=_env_float("POLY_COUNTER_REVERSAL_MIN_LEADER_PRICE", cls.min_leader_price),
            max_leader_price=_env_float("POLY_COUNTER_REVERSAL_MAX_LEADER_PRICE", cls.max_leader_price),
            max_counter_price=_env_float("POLY_COUNTER_REVERSAL_MAX_COUNTER_PRICE", cls.max_counter_price),
            min_counter_price=_env_float("POLY_COUNTER_REVERSAL_MIN_COUNTER_PRICE", cls.min_counter_price),
            min_adverse_5s_bps=_env_float("POLY_COUNTER_REVERSAL_MIN_ADVERSE_5S_BPS", cls.min_adverse_5s_bps),
            max_abs_distance_bps=_env_float("POLY_COUNTER_REVERSAL_MAX_ABS_DISTANCE_BPS", cls.max_abs_distance_bps),
            max_distance_vs_range_60s=_env_float("POLY_COUNTER_REVERSAL_MAX_DISTANCE_VS_RANGE_60S", cls.max_distance_vs_range_60s),
            max_source_divergence_bps=_env_float("POLY_COUNTER_REVERSAL_MAX_SOURCE_DIVERGENCE_BPS", cls.max_source_divergence_bps),
            max_spread=_env_float("POLY_COUNTER_REVERSAL_MAX_SPREAD", cls.max_spread),
            min_depth_top3=_env_float("POLY_COUNTER_REVERSAL_MIN_DEPTH_TOP3", cls.min_depth_top3),
            qty=_env_float("POLY_COUNTER_REVERSAL_QTY", cls.qty),
            entry_order_type=os.getenv("POLY_COUNTER_REVERSAL_ENTRY_ORDER_TYPE", cls.entry_order_type),
            entry_timeout_secs=_env_float("POLY_COUNTER_REVERSAL_ENTRY_TIMEOUT_SECS", cls.entry_timeout_secs),
            target_price=_env_float("POLY_COUNTER_REVERSAL_TARGET_PRICE", cls.target_price),
            runner_price=_env_float("POLY_COUNTER_REVERSAL_RUNNER_PRICE", cls.runner_price),
            stop_price=_env_float("POLY_COUNTER_REVERSAL_STOP_PRICE", cls.stop_price),
            flatten_deadline_secs=_env_int("POLY_COUNTER_REVERSAL_FLATTEN_DEADLINE_SECS", cls.flatten_deadline_secs),
            exit_repost_secs=_env_float("POLY_COUNTER_REVERSAL_EXIT_REPOST_SECS", cls.exit_repost_secs),
            poll_secs=max(0.25, _env_float("POLY_COUNTER_REVERSAL_POLL_SECS", cls.poll_secs)),
        )


@dataclass
class CounterReversalTradeStateV1:
    mode: str = "idle"  # idle | pending_entry | open_position | pending_exit
    event_slug: Optional[str] = None
    leader_side: Optional[str] = None
    side: Optional[str] = None
    token_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    entry_price: Optional[float] = None
    entry_qty_requested: float = 0.0
    entry_qty_filled: float = 0.0
    exit_qty_filled: float = 0.0
    best_bid: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    last_reason: Optional[str] = None

    @property
    def remaining_position_qty(self) -> float:
        return round(max(0.0, float(self.entry_qty_filled) - float(self.exit_qty_filled)), 6)


def _load_state(path: Path) -> CounterReversalTradeStateV1:
    if not path.exists():
        return CounterReversalTradeStateV1()
    try:
        return CounterReversalTradeStateV1(**json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return CounterReversalTradeStateV1()


def _save_state(path: Path, trade: CounterReversalTradeStateV1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(trade), ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_state(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _side_book(snap: dict, side: str) -> dict:
    if side == "UP":
        return snap.get("up") or {}
    if side == "DOWN":
        return snap.get("down") or {}
    return {}


def _token_id_for_side(snap: dict, side: str) -> str:
    return str(_side_book(snap, side).get("token_id") or "")


def _tick_size_for_side(snap: dict, side: str) -> float:
    return max(0.001, _safe_float(_side_book(snap, side).get("tick_size"), 0.01))


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
    if status in ("filled", "matched", "canceled", "cancelled", "closed", "resolved", "rejected"):
        return None
    return broker.cancel_order(order_id)


def _token_balance_qty(broker, token_id: Optional[str]) -> float:
    if not token_id:
        return 0.0
    try:
        payload = broker.get_balance_allowance(asset_type="CONDITIONAL", token_id=token_id)
        return round(float(payload.get("balance") or 0.0) / 1_000_000.0, 6)
    except Exception:
        return 0.0


def _fetch_active_bid(token_id: Optional[str]) -> float:
    if not token_id:
        return 0.0
    try:
        books = fetch_books_for_tokens([token_id])
    except Exception:
        return 0.0
    bids = ((books[0] if books else {}) or {}).get("bids") or []
    return _safe_float((bids[0] or {}).get("price"), 0.0) if bids else 0.0


def _is_flat(qty: float) -> bool:
    return abs(float(qty)) <= 0.000001


def _ask(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_ask" if side == "UP" else "down_ask"), 0.0)


def _bid(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _spread(executable: Optional[dict], side: str) -> float:
    return round(max(0.0, _ask(executable, side) - _bid(executable, side)), 6)


def _counter_signal(*, snap: dict, executable: Optional[dict], scalp: dict, secs_to_end: Optional[int], cfg: CounterReversalConfigV1) -> dict:
    up_ask = _ask(executable, "UP")
    down_ask = _ask(executable, "DOWN")
    up_bid = _bid(executable, "UP")
    down_bid = _bid(executable, "DOWN")
    distance_bps = _safe_float(scalp.get("distance_from_open_bps"), 0.0)
    reference_price = _safe_float(scalp.get("reference_price"), 0.0)
    opening_price = _safe_float(scalp.get("opening_reference_price"), 0.0)
    distance_usd = abs(reference_price - opening_price) if reference_price > 0 and opening_price > 0 else 0.0
    range_60s = _safe_float(scalp.get("spot_range_60s_usd"), 0.0)
    distance_vs_range = round(distance_usd / range_60s, 6) if range_60s > 0 else None
    spot_delta_5s = _safe_float(scalp.get("spot_delta_5s_bps"), 0.0)
    source_div = _safe_float(scalp.get("source_divergence_bps"), 0.0)
    depth = _safe_float(scalp.get("combined_depth_top3"), 0.0)

    leader_side = "UP" if distance_bps > 0 else ("DOWN" if distance_bps < 0 else None)
    counter_side = "DOWN" if leader_side == "UP" else ("UP" if leader_side == "DOWN" else None)
    leader_price = up_ask if leader_side == "UP" else down_ask if leader_side == "DOWN" else 0.0
    counter_price = down_ask if counter_side == "DOWN" else up_ask if counter_side == "UP" else 0.0
    adverse_5s = -spot_delta_5s if leader_side == "UP" else spot_delta_5s if leader_side == "DOWN" else 0.0
    fragile_distance = abs(distance_bps) <= cfg.max_abs_distance_bps or (
        distance_vs_range is not None and distance_vs_range <= cfg.max_distance_vs_range_60s
    )

    reasons = []
    if secs_to_end is None or secs_to_end < cfg.min_secs_to_end or secs_to_end > cfg.max_secs_to_end:
        reasons.append("outside_time_window")
    if leader_side is None:
        reasons.append("not_on_one_side_of_open")
    if leader_price < cfg.min_leader_price or leader_price > cfg.max_leader_price:
        reasons.append("leader_price_outside_band")
    if counter_price < cfg.min_counter_price or counter_price > cfg.max_counter_price:
        reasons.append("counter_price_outside_band")
    if adverse_5s < cfg.min_adverse_5s_bps:
        reasons.append("adverse_5s_too_small")
    if not fragile_distance:
        reasons.append("distance_not_fragile")
    if source_div > cfg.max_source_divergence_bps:
        reasons.append("source_divergence_too_high")
    if counter_side and _spread(executable, counter_side) > cfg.max_spread:
        reasons.append("counter_spread_too_wide")
    if depth < cfg.min_depth_top3:
        reasons.append("low_depth")

    return {
        "setup": "counter_reversal_near_resolved",
        "allow": not reasons,
        "reason": "allow" if not reasons else "|".join(reasons),
        "leader_side": leader_side,
        "side": counter_side,
        "leader_price": leader_price,
        "counter_price": counter_price,
        "entry_price": counter_price,
        "up_ask": up_ask,
        "up_bid": up_bid,
        "down_ask": down_ask,
        "down_bid": down_bid,
        "secs_to_end": secs_to_end,
        "distance_from_open_bps": distance_bps,
        "distance_to_open_usd": round(distance_usd, 4),
        "spot_range_60s_usd": range_60s,
        "distance_vs_spot_range_60s": distance_vs_range,
        "spot_delta_5s_bps": spot_delta_5s,
        "adverse_5s_bps": round(adverse_5s, 4),
        "source_divergence_bps": source_div,
        "combined_depth_top3": depth,
        "token_id": _token_id_for_side(snap, counter_side or ""),
    }


def _post_entry(broker, *, signal: dict, slug: str, cfg: CounterReversalConfigV1, now: float) -> CounterReversalTradeStateV1:
    side = str(signal.get("side") or "")
    token_id = str(signal.get("token_id") or "")
    entry_price = _safe_float(signal.get("entry_price"), 0.0)
    if not token_id or side not in ("UP", "DOWN"):
        raise RuntimeError("missing counter reversal token/side")
    req = BrokerOrderRequest(
        token_id=token_id,
        side="BUY",
        price=entry_price,
        size=float(cfg.qty),
        order_type=str(cfg.entry_order_type or "FAK").upper(),
        market_slug=slug,
        outcome=side,
        client_order_key=f"counter_reversal:entry:{int(now)}:{side}",
    )
    order = broker.place_limit_order(req)
    return CounterReversalTradeStateV1(
        mode="pending_entry",
        event_slug=slug,
        leader_side=str(signal.get("leader_side") or ""),
        side=side,
        token_id=token_id,
        entry_order_id=order.order_id,
        entry_price=entry_price,
        entry_qty_requested=float(cfg.qty),
        created_at=now,
        updated_at=now,
        last_reason="entry_posted",
    )


def _sync_trade(broker, trade: CounterReversalTradeStateV1) -> CounterReversalTradeStateV1:
    entry = _get_order_status(broker, trade.entry_order_id)
    if entry is not None:
        trade.entry_qty_filled = max(trade.entry_qty_filled, _safe_float(getattr(entry, "size_matched", None), 0.0))
    exit_order = _get_order_status(broker, trade.exit_order_id)
    if exit_order is not None:
        trade.exit_qty_filled = max(trade.exit_qty_filled, _safe_float(getattr(exit_order, "size_matched", None), 0.0))
    balance = _token_balance_qty(broker, trade.token_id)
    if balance > 0:
        trade.entry_qty_filled = max(trade.entry_qty_filled, balance + trade.exit_qty_filled)
    if trade.mode == "pending_entry" and trade.entry_qty_filled > 0:
        trade.mode = "open_position"
    if trade.mode != "idle" and _is_flat(balance) and trade.entry_qty_filled > 0:
        return CounterReversalTradeStateV1()
    return trade


def _post_exit(broker, trade: CounterReversalTradeStateV1, *, exit_price: float, reason: str, now: float) -> CounterReversalTradeStateV1:
    qty = _token_balance_qty(broker, trade.token_id)
    if _is_flat(qty):
        qty = trade.remaining_position_qty
    if _is_flat(qty):
        return CounterReversalTradeStateV1()
    try:
        broker.update_balance_allowance(asset_type="CONDITIONAL", token_id=trade.token_id)
    except Exception:
        pass
    req = BrokerOrderRequest(
        token_id=trade.token_id or "",
        side="SELL",
        price=round(max(0.01, float(exit_price)), 6),
        size=float(qty),
        order_type="FAK",
        market_slug=trade.event_slug,
        outcome=trade.side,
        client_order_key=f"counter_reversal:exit:{reason}:{int(now)}:{trade.side}",
    )
    order = broker.place_limit_order(req)
    trade.exit_order_id = order.order_id
    trade.mode = "pending_exit"
    trade.updated_at = now
    trade.last_reason = f"exit_posted:{reason}"
    return trade


def _paper_manage(trade: CounterReversalTradeStateV1, *, signal: dict, bid_now: float, secs_to_end: Optional[int], cfg: CounterReversalConfigV1, now: float) -> CounterReversalTradeStateV1:
    if trade.mode == "pending_entry" and _safe_float(signal.get("entry_price"), 0.0) <= _safe_float(trade.entry_price, 0.0):
        trade.mode = "open_position"
        trade.entry_qty_filled = trade.entry_qty_requested
        trade.last_reason = "paper_entry_filled"
    if trade.mode != "open_position":
        return trade
    trade.best_bid = max(trade.best_bid, bid_now)
    exit_reason = _exit_reason(trade, signal=signal, bid_now=bid_now, secs_to_end=secs_to_end, cfg=cfg, now=now)
    if exit_reason:
        trade.mode = "idle"
        trade.exit_qty_filled = trade.entry_qty_filled
        trade.updated_at = now
        trade.last_reason = f"paper_exit:{exit_reason}:{bid_now}"
    return trade


def _exit_reason(trade: CounterReversalTradeStateV1, *, signal: dict, bid_now: float, secs_to_end: Optional[int], cfg: CounterReversalConfigV1, now: float) -> Optional[str]:
    entry = _safe_float(trade.entry_price, 0.0)
    if bid_now >= cfg.runner_price:
        return "runner_target"
    if bid_now >= cfg.target_price:
        return "target"
    if entry > 0 and bid_now > 0 and bid_now <= cfg.stop_price and now - trade.created_at >= 3.0:
        return "cheap_option_stop"
    if secs_to_end is not None and secs_to_end <= cfg.flatten_deadline_secs:
        return "deadline_flatten"
    leader_side = trade.leader_side
    distance_bps = _safe_float(signal.get("distance_from_open_bps"), 0.0)
    if leader_side == "UP" and distance_bps >= cfg.max_abs_distance_bps and _safe_float(signal.get("adverse_5s_bps"), 0.0) <= 0:
        return "leader_recovered"
    if leader_side == "DOWN" and distance_bps <= -cfg.max_abs_distance_bps and _safe_float(signal.get("adverse_5s_bps"), 0.0) <= 0:
        return "leader_recovered"
    return None


def monitor_live_counter_reversal_real_v1(*, duration_seconds: Optional[int] = None, execute: bool = False, log_dir: Optional[str] = None) -> None:
    load_dotenv()
    guarded = load_live_guarded_config()
    broker_status = load_broker_env()
    cfg = CounterReversalConfigV1.from_env()
    scalp_cfg = CurrentScalpConfigV1()
    run_for = int(duration_seconds or _env_int("POLY_COUNTER_REVERSAL_RUN_SECONDS", 1800))
    session_dir = Path(log_dir) if log_dir else _build_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "counter_reversal_real.jsonl"
    exception_path = session_dir / "exception.log"
    state_path = _state_path()
    session_id = session_dir.name

    print("[COUNTER_REVERSAL_PARAMS]", {"execute": execute, "run_for": run_for, "log_path": str(log_path), **cfg.as_dict()})
    print("[BROKER_ENV]", broker_status.as_dict())
    print("[LIVE_GUARDED_CONFIG]", guarded.as_dict())

    broker = None
    trade = _load_state(state_path) if execute else CounterReversalTradeStateV1()
    if execute:
        if not guarded.enabled or guarded.shadow_only or not guarded.real_posts_enabled:
            print("[GUARD] Real execution requires guarded enabled, shadow_only=false, real_posts_enabled=true.")
            return
        if not _env_bool("POLY_COUNTER_REVERSAL_REAL_ENABLED", False):
            print("[GUARD] Set POLY_COUNTER_REVERSAL_REAL_ENABLED=true to arm real counter reversal.")
            return
        if not broker_status.ready_for_real_smoke:
            print("[GUARD] Broker env missing required credentials.")
            return
        if cfg.qty < 5:
            print("[GUARD] Polymarket real orders require qty >= 5.")
            return
        broker = PolymarketBrokerV3.from_env()
        health = broker.healthcheck()
        print("[BROKER_HEALTH]", health.as_dict())
        if not health.ok:
            return
        startup_orders = broker.get_open_orders()[:50]
        print("[BROKER_OPEN_ORDERS_STARTUP]", [o.as_dict() for o in startup_orders])
        allowed_ids = {x for x in (trade.entry_order_id, trade.exit_order_id) if x}
        startup_ids = {o.order_id for o in startup_orders}
        if startup_ids - allowed_ids:
            print("[GUARD] Refusing to start with open orders not owned by counter reversal state.")
            return
        trade = _sync_trade(broker, trade)
        _save_state(state_path, trade)

    current_scalp = CurrentScalpResearchV1(cfg=scalp_cfg)
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
                current_open_reference = {"slug": current_item.get("slug"), "price": open_ref.get("open_price"), "event_start_time": event_start_time}

            reference = fetch_external_btc_reference_v1() if current_item else {}
            scalp = (
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
                else {"allow": False, "reason": "missing_current"}
            )
            signal = _counter_signal(snap=current_snap, executable=current_exec, scalp=scalp, secs_to_end=current_secs, cfg=cfg)
            if current_item:
                signal["event_slug"] = current_item.get("slug")

            bid_now = _bid(current_exec, trade.side or "") if trade.side else 0.0
            if execute and trade.token_id:
                bid_now = max(bid_now, _fetch_active_bid(trade.token_id))
                trade = _sync_trade(broker, trade)
            elif not execute:
                trade = _paper_manage(trade, signal=signal, bid_now=bid_now, secs_to_end=current_secs, cfg=cfg, now=now)
            current_almost_resolved_active = _state_file_active(_current_almost_resolved_state_path())

            snapshot = {
                "type": "snapshot",
                "ts": now,
                "session_id": session_id,
                "execute": execute,
                "current_slug": current_item.get("slug") if current_item else None,
                "current_secs": current_secs,
                "current_exec_reason": current_exec_reason,
                "reference": reference,
                "current_scalp_context": scalp,
                "signal": signal,
                "trade": asdict(trade),
                "active_bid": bid_now,
                "current_almost_resolved_active": current_almost_resolved_active,
            }
            _append_jsonl(log_path, snapshot)
            print(
                f"[COUNTER_REVERSAL] secs={current_secs} allow={signal.get('allow')} reason={signal.get('reason')} "
                f"leader={signal.get('leader_side')} {signal.get('leader_price')} counter={signal.get('side')} {signal.get('counter_price')} mode={trade.mode}"
            )

            if trade.mode == "idle" and current_item and signal.get("allow") and not current_almost_resolved_active:
                if execute:
                    trade = _post_entry(broker, signal=signal, slug=str(current_item.get("slug") or ""), cfg=cfg, now=now)
                    _save_state(state_path, trade)
                else:
                    trade = CounterReversalTradeStateV1(
                        mode="pending_entry",
                        event_slug=str(current_item.get("slug") or ""),
                        leader_side=str(signal.get("leader_side") or ""),
                        side=str(signal.get("side") or ""),
                        token_id=str(signal.get("token_id") or ""),
                        entry_price=_safe_float(signal.get("entry_price"), 0.0),
                        entry_qty_requested=cfg.qty,
                        created_at=now,
                        updated_at=now,
                        last_reason="paper_entry_posted",
                    )
                _append_jsonl(log_path, {"type": "enter", "ts": now, "session_id": session_id, "execute": execute, "signal": signal, "trade": asdict(trade)})
            if trade.mode == "idle" and current_item and signal.get("allow") and current_almost_resolved_active:
                _append_jsonl(
                    log_path,
                    {
                        "type": "entry_blocked",
                        "ts": now,
                        "session_id": session_id,
                        "execute": execute,
                        "reason": "current_almost_resolved_active",
                        "signal": signal,
                    },
                )

            if execute and trade.mode == "pending_entry":
                if trade.entry_qty_filled > 0:
                    _cancel_if_live(broker, trade.entry_order_id)
                    trade.mode = "open_position"
                    trade.updated_at = now
                    trade.last_reason = "entry_fill_detected"
                    _save_state(state_path, trade)
                    _append_jsonl(log_path, {"type": "fill", "ts": now, "session_id": session_id, "trade": asdict(trade)})
                elif now - trade.created_at >= cfg.entry_timeout_secs:
                    cancel = _cancel_if_live(broker, trade.entry_order_id)
                    _append_jsonl(log_path, {"type": "entry_cancel", "ts": now, "session_id": session_id, "cancel": cancel, "trade": asdict(trade)})
                    trade = CounterReversalTradeStateV1()
                    _clear_state(state_path)

            if execute and trade.mode == "open_position":
                reason = _exit_reason(trade, signal=signal, bid_now=bid_now, secs_to_end=current_secs, cfg=cfg, now=now)
                if reason:
                    trade = _post_exit(broker, trade, exit_price=bid_now if bid_now > 0 else 0.01, reason=reason, now=now)
                    _save_state(state_path, trade)
                    _append_jsonl(log_path, {"type": "exit_posted", "ts": now, "session_id": session_id, "reason": reason, "trade": asdict(trade)})

            if execute and trade.mode == "pending_exit":
                balance = _token_balance_qty(broker, trade.token_id)
                if _is_flat(balance):
                    _append_jsonl(log_path, {"type": "flat", "ts": now, "session_id": session_id, "trade": asdict(trade)})
                    trade = CounterReversalTradeStateV1()
                    _clear_state(state_path)
                elif now - trade.updated_at >= cfg.exit_repost_secs:
                    _cancel_if_live(broker, trade.exit_order_id)
                    trade = _post_exit(broker, trade, exit_price=bid_now if bid_now > 0 else 0.01, reason="repost", now=now)
                    _save_state(state_path, trade)
                    _append_jsonl(log_path, {"type": "exit_repost", "ts": now, "session_id": session_id, "trade": asdict(trade)})

            time.sleep(cfg.poll_secs)
        except Exception as exc:
            trace = traceback.format_exc()
            exception_path.write_text(trace, encoding="utf-8")
            _append_jsonl(log_path, {"type": "exception", "ts": time.time(), "session_id": session_id, "error": f"{type(exc).__name__}: {exc}", "traceback": trace, "trade": asdict(trade)})
            if execute and broker is not None and trade.mode != "idle":
                try:
                    _cancel_if_live(broker, trade.entry_order_id)
                    _cancel_if_live(broker, trade.exit_order_id)
                    bid_now = _fetch_active_bid(trade.token_id)
                    _post_exit(broker, trade, exit_price=bid_now if bid_now > 0 else 0.01, reason="panic", now=time.time())
                except Exception:
                    pass
            raise

    if execute and broker is not None and trade.mode == "pending_entry":
        _cancel_if_live(broker, trade.entry_order_id)
        _clear_state(state_path)
    elif execute and broker is not None and trade.mode in ("open_position", "pending_exit"):
        bid_now = _fetch_active_bid(trade.token_id)
        trade = _post_exit(broker, trade, exit_price=bid_now if bid_now > 0 else 0.01, reason="shutdown_flatten", now=time.time())
        _save_state(state_path, trade)
