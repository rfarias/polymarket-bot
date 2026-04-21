from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from market.broker_types import BrokerOrderRequest
from market.continuation_filter_v1 import ContinuationRiskFilterV1
from market.live_guarded_config import load_live_guarded_config
from market.polymarket_broker_v3 import PolymarketBrokerV3
from market.rest_5m_shadow_public_v5 import (
    _build_slot_bundle,
    _compute_executable_metrics,
    _current_secs_to_end,
    _fetch_slot_state,
    _slot_snapshot,
)


@dataclass
class ScalpStateV1:
    mode: str = "idle"  # idle | pending_entry | open_position | pending_exit | done
    slot_name: Optional[str] = None
    event_slug: Optional[str] = None
    outcome: Optional[str] = None
    token_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    entry_price: Optional[float] = None
    entry_filled_qty: float = 0.0
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_reason: Optional[str] = None


@dataclass
class ScalpConfigV1:
    enabled: bool = False
    run_seconds: int = 600
    loop_sleep_secs: int = 2
    allowed_slots: List[str] = None
    qty: int = 1
    stretch_price_max: float = 0.36
    max_spread: float = 0.03
    min_depth_top3: float = 8.0
    target_ticks: int = 1
    stop_ticks: int = 2
    entry_timeout_secs: int = 12
    position_timeout_secs: int = 20
    min_secs_to_end: int = 45


def _runtime_state_path() -> Path:
    return Path("runtime") / "polymarket_scalp_state_v1.json"


def _save_state(state: ScalpStateV1) -> Dict:
    path = _runtime_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(path), "mode": state.mode}


def _clear_state() -> Dict:
    path = _runtime_state_path()
    if path.exists():
        path.unlink()
        return {"ok": True, "action": "cleared", "path": str(path)}
    return {"ok": True, "action": "already_missing", "path": str(path)}


def _load_state() -> Optional[ScalpStateV1]:
    path = _runtime_state_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ScalpStateV1(**raw)
    except Exception:
        return None


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


def _load_scalp_cfg_v1() -> ScalpConfigV1:
    allowed = os.getenv("POLY_SCALP_ALLOWED_SLOTS", "next_1")
    allowed_slots = [x.strip() for x in allowed.split(",") if x.strip() in ("current", "next_1")]
    if not allowed_slots:
        allowed_slots = ["next_1"]
    return ScalpConfigV1(
        enabled=str(os.getenv("POLY_SCALP_ENABLED", "false")).lower() == "true",
        run_seconds=_env_int("POLY_SCALP_RUN_SECONDS", 600),
        loop_sleep_secs=max(1, _env_int("POLY_SCALP_LOOP_SLEEP_SECS", 2)),
        allowed_slots=allowed_slots,
        qty=max(1, _env_int("POLY_SCALP_QTY", 1)),
        stretch_price_max=_env_float("POLY_SCALP_STRETCH_PRICE_MAX", 0.36),
        max_spread=_env_float("POLY_SCALP_MAX_SPREAD", 0.03),
        min_depth_top3=_env_float("POLY_SCALP_MIN_DEPTH_TOP3", 8.0),
        target_ticks=max(1, _env_int("POLY_SCALP_TARGET_TICKS", 1)),
        stop_ticks=max(1, _env_int("POLY_SCALP_STOP_TICKS", 2)),
        entry_timeout_secs=max(3, _env_int("POLY_SCALP_ENTRY_TIMEOUT_SECS", 12)),
        position_timeout_secs=max(5, _env_int("POLY_SCALP_POSITION_TIMEOUT_SECS", 20)),
        min_secs_to_end=max(20, _env_int("POLY_SCALP_MIN_SECS_TO_END", 45)),
    )


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _sum_depth(levels: Optional[List[Dict]]) -> float:
    if not levels:
        return 0.0
    total = 0.0
    for lvl in levels[:3]:
        total += _safe_float((lvl or {}).get("size"), 0.0)
    return round(total, 6)


def _choose_entry_candidate(snap: Dict, executable_metrics: Dict, continuation: Dict, cfg: ScalpConfigV1) -> Dict:
    up = snap.get("up") or {}
    down = snap.get("down") or {}

    up_ask = _safe_float(executable_metrics.get("up_ask"), 0.0)
    down_ask = _safe_float(executable_metrics.get("down_ask"), 0.0)
    up_bid = _safe_float(executable_metrics.get("up_bid"), 0.0)
    down_bid = _safe_float(executable_metrics.get("down_bid"), 0.0)

    if up_ask <= down_ask:
        outcome = "UP"
        token_id = str(up.get("token_id") or "")
        entry_price = up_ask
        bid_now = up_bid
        best_bid = _safe_float(up.get("best_bid"), 0.0)
        best_ask = _safe_float(up.get("best_ask"), 0.0)
    else:
        outcome = "DOWN"
        token_id = str(down.get("token_id") or "")
        entry_price = down_ask
        bid_now = down_bid
        best_bid = _safe_float(down.get("best_bid"), 0.0)
        best_ask = _safe_float(down.get("best_ask"), 0.0)

    spread = round(max(0.0, best_ask - best_bid), 6)
    top3_depth = round(_sum_depth((up.get("top_bids") or [])) + _sum_depth((down.get("top_bids") or [])), 6)

    reasons = []
    allow = True

    if not token_id:
        allow = False
        reasons.append("missing_token_id")
    if entry_price <= 0:
        allow = False
        reasons.append("invalid_entry_price")
    if entry_price > cfg.stretch_price_max:
        allow = False
        reasons.append(f"not_stretched entry_price={entry_price} > {cfg.stretch_price_max}")
    if spread > cfg.max_spread:
        allow = False
        reasons.append(f"spread_too_wide spread={spread} > {cfg.max_spread}")
    if top3_depth < cfg.min_depth_top3:
        allow = False
        reasons.append(f"low_depth_top3 depth={top3_depth} < {cfg.min_depth_top3}")

    cont_label = continuation.get("label")
    if cont_label in ("continuation_risk_medium", "continuation_risk_high"):
        allow = False
        reasons.append(f"continuation_not_reversal label={cont_label}")

    mono = _safe_float(continuation.get("monotonic_ratio_60s"), 0.0)
    accel = _safe_float(continuation.get("accel"), 0.0)
    abs_d60 = abs(_safe_float(continuation.get("delta60"), 0.0))
    imbalance = abs(_safe_float(continuation.get("depth_imbalance_top3"), 0.0))

    if mono > 0.78:
        allow = False
        reasons.append(f"still_monotonic mono={mono}")
    if accel > 0.015:
        allow = False
        reasons.append(f"movement_accelerating accel={accel}")
    if abs_d60 < 0.015:
        allow = False
        reasons.append(f"no_prior_extension delta60={abs_d60}")
    if imbalance > 0.30:
        allow = False
        reasons.append(f"imbalance_extreme={imbalance}")

    return {
        "allow": allow,
        "reasons": reasons,
        "outcome": outcome,
        "token_id": token_id,
        "entry_price": entry_price,
        "bid_now": bid_now,
        "spread": spread,
        "top3_depth": top3_depth,
    }


def _tick_size_from_snap(snap: Dict, outcome: str) -> float:
    side = (snap.get("up") if outcome == "UP" else snap.get("down")) or {}
    tick = _safe_float(side.get("tick_size"), 0.01)
    return max(0.001, tick)


def _find_order_in_open(broker, order_id: str):
    if not order_id:
        return None
    try:
        for order in broker.get_open_orders()[:50]:
            if order.order_id == order_id:
                return order
    except Exception:
        return None
    return None


def _get_order_status(broker, order_id: str):
    if not order_id:
        return None
    try:
        order = broker.get_order(order_id)
        if order is not None:
            return order
    except Exception:
        pass
    return _find_order_in_open(broker, order_id)


def _blocking_open_orders(broker):
    try:
        return broker.get_open_orders()[:50]
    except Exception:
        return []


def _reset_runtime_state(now: float) -> ScalpStateV1:
    return ScalpStateV1(mode="idle", created_at=now, updated_at=now)


def _clear_and_reset(now: float) -> ScalpStateV1:
    print("[SCALP_STATE_CLEAR]", _clear_state())
    return _reset_runtime_state(now)


def _post_exit_order(broker, state: ScalpStateV1, *, bid_now: float, now: float, reason: str) -> ScalpStateV1:
    exit_qty = round(float(state.entry_filled_qty or 0.0), 6)
    if exit_qty <= 0:
        print(f"[SCALP_EXIT_BLOCK] reason={reason} invalid_exit_qty={exit_qty}")
        return _clear_and_reset(now)

    req = BrokerOrderRequest(
        token_id=state.token_id or "",
        side="SELL",
        price=bid_now,
        size=exit_qty,
        market_slug=state.event_slug,
        outcome=state.outcome,
        client_order_key=f"scalp_exit:{reason}:{int(now)}:{state.outcome}",
    )
    exit_order = broker.place_limit_order(req)
    print(
        f"[SCALP_EXIT_POSTED] reason={reason} outcome={state.outcome} bid_now={bid_now} "
        f"entry={state.entry_price} qty={exit_qty} exit_order_id={exit_order.order_id}"
    )
    state.mode = "pending_exit"
    state.exit_order_id = exit_order.order_id
    state.updated_at = now
    state.last_reason = f"exit_posted:{reason}"
    print("[SCALP_STATE_FLUSH]", _save_state(state))
    return state


def monitor_live_scalp_reversal_v1(duration_seconds: Optional[int] = None) -> None:
    guarded = load_live_guarded_config()
    cfg = _load_scalp_cfg_v1()
    print("[SCALP_CONFIG]", asdict(cfg))

    if not cfg.enabled:
        print("[SCALP_GUARD] Disabled. Set POLY_SCALP_ENABLED=true")
        return
    if guarded.shadow_only or not guarded.real_posts_enabled:
        print("[SCALP_GUARD] Requires guarded real mode (shadow_only=false and real_posts_enabled=true)")
        return

    broker = PolymarketBrokerV3.from_env()
    health = broker.healthcheck()
    print("[BROKER_HEALTH]", health.as_dict())
    if not health.ok:
        print("[SCALP_GUARD] Broker healthcheck failed")
        return

    state = _load_state() or ScalpStateV1(mode="idle", created_at=time.time(), updated_at=time.time())
    print("[SCALP_STATE_RESTORE]", asdict(state))

    startup_open_orders = _blocking_open_orders(broker)
    if startup_open_orders:
        print("[SCALP_GUARD] Startup blocked because broker account already has open orders")
        print("[SCALP_OPEN_ORDERS_STARTUP]", [o.as_dict() for o in startup_open_orders])
        return

    if state.mode != "idle":
        print("[SCALP_RECOVERY] stale local scalp state without open broker orders; clearing local state")
        state = _clear_and_reset(time.time())

    print("[SCALP_STATE_FLUSH]", _save_state(state))

    slot_bundle = _build_slot_bundle()
    cont = ContinuationRiskFilterV1()

    run_for = int(duration_seconds or cfg.run_seconds)
    started_at = time.time()

    while time.time() - started_at < run_for:
        now = time.time()
        slot_state = _fetch_slot_state(slot_bundle)

        candidates = []
        for slot_name in cfg.allowed_slots:
            item = slot_bundle["queue"].get(slot_name)
            secs_to_end = _current_secs_to_end(item.get("seconds_to_end") if item else None, started_at)
            if secs_to_end is None or secs_to_end < cfg.min_secs_to_end:
                continue

            snap = _slot_snapshot(slot_state, slot_name)
            executable, reason = _compute_executable_metrics(snap)
            if executable is None:
                print(f"[SCALP_BLOCK] slot={slot_name} executable_missing reason={reason}")
                continue

            continuation = cont.update_and_classify(slot_name=slot_name, snap=snap, now_ts=now)
            decision = _choose_entry_candidate(snap, executable, continuation, cfg)
            print(
                f"[SCALP_SIGNAL] slot={slot_name} secs={secs_to_end} "
                f"label={continuation.get('label')} score={continuation.get('score')} "
                f"delta30={continuation.get('delta30')} delta60={continuation.get('delta60')} "
                f"imbalance={continuation.get('depth_imbalance_top3')} allow={decision['allow']}"
            )
            if not decision["allow"]:
                print(f"[SCALP_BLOCK] slot={slot_name} reasons={decision['reasons']}")
                continue

            candidates.append((slot_name, item, snap, executable, continuation, decision, secs_to_end))

        # lifecycle: single scalp only
        if state.mode == "idle" and candidates:
            slot_name, item, snap, _exec, _cont, decision, _secs = candidates[0]
            tick = _tick_size_from_snap(snap, decision["outcome"])
            target = round(decision["entry_price"] + cfg.target_ticks * tick, 6)
            stop = round(max(0.001, decision["entry_price"] - cfg.stop_ticks * tick), 6)

            req = BrokerOrderRequest(
                token_id=decision["token_id"],
                side="BUY",
                price=float(decision["entry_price"]),
                size=float(cfg.qty),
                market_slug=item["slug"],
                outcome=decision["outcome"],
                client_order_key=f"scalp:{int(now)}:{slot_name}:{decision['outcome']}",
            )
            order = broker.place_limit_order(req)
            state = ScalpStateV1(
                mode="pending_entry",
                slot_name=slot_name,
                event_slug=item["slug"],
                outcome=decision["outcome"],
                token_id=decision["token_id"],
                entry_order_id=order.order_id,
                entry_price=decision["entry_price"],
                entry_filled_qty=0.0,
                target_price=target,
                stop_price=stop,
                created_at=now,
                updated_at=now,
                last_reason="entry_posted",
            )
            print(
                f"[SCALP_ENTRY_ALLOWED] slot={slot_name} outcome={decision['outcome']} "
                f"entry={decision['entry_price']} target={target} stop={stop} qty={cfg.qty} order_id={order.order_id}"
            )
            print("[SCALP_STATE_FLUSH]", _save_state(state))

        elif state.mode == "pending_entry":
            ord_open = _get_order_status(broker, state.entry_order_id or "")
            filled = _safe_float(ord_open.size_matched, 0.0) if ord_open else state.entry_filled_qty
            state.entry_filled_qty = max(state.entry_filled_qty, filled)
            elapsed = now - state.created_at

            if state.entry_filled_qty > 0:
                state.mode = "open_position"
                state.updated_at = now
                state.last_reason = "entry_filled_or_partial"
                if ord_open and ord_open.remaining_size > 0:
                    cancel_resp = broker.cancel_order(state.entry_order_id)
                    print(f"[SCALP_ENTRY_CANCEL_REMAINDER] {cancel_resp}")
                print(f"[SCALP_ENTRY_FILLED] qty={state.entry_filled_qty} order_id={state.entry_order_id}")
                print("[SCALP_STATE_FLUSH]", _save_state(state))
            elif elapsed >= cfg.entry_timeout_secs:
                cancel_resp = broker.cancel_order(state.entry_order_id)
                print(f"[SCALP_EXIT_TIMEOUT_NO_FILL] cancel_resp={cancel_resp}")
                state = _clear_and_reset(now)

        elif state.mode == "open_position":
            slot_snap = _slot_snapshot(slot_state, state.slot_name or "next_1")
            executable, _ = _compute_executable_metrics(slot_snap)
            if executable is None:
                print("[SCALP_HOLD] executable unavailable while open position")
                time.sleep(cfg.loop_sleep_secs)
                continue

            bid_now = _safe_float(executable.get("up_bid" if state.outcome == "UP" else "down_bid"), 0.0)
            elapsed = now - state.updated_at

            if bid_now >= _safe_float(state.target_price, 0.0):
                state = _post_exit_order(broker, state, bid_now=bid_now, now=now, reason="tp")

            elif bid_now <= _safe_float(state.stop_price, 0.0):
                state = _post_exit_order(broker, state, bid_now=bid_now, now=now, reason="stop")

            elif elapsed >= cfg.position_timeout_secs:
                state = _post_exit_order(broker, state, bid_now=bid_now, now=now, reason="timeout")

        elif state.mode == "pending_exit":
            exit_order = _get_order_status(broker, state.exit_order_id or "")
            elapsed = now - state.updated_at

            if exit_order is None:
                print(f"[SCALP_EXIT_DONE] order_id={state.exit_order_id} not returned by broker anymore; assuming terminal")
                state = _clear_and_reset(now)
            else:
                status = str(exit_order.status or "").lower()
                matched = _safe_float(exit_order.size_matched, 0.0)
                print(
                    f"[SCALP_EXIT_WAIT] order_id={state.exit_order_id} status={status} "
                    f"matched={matched} remaining={exit_order.remaining_size}"
                )
                if status in ("filled", "closed", "resolved"):
                    state = _clear_and_reset(now)
                elif elapsed >= cfg.position_timeout_secs:
                    cancel_resp = broker.cancel_order(state.exit_order_id)
                    print(f"[SCALP_EXIT_CANCEL_TIMEOUT] cancel_resp={cancel_resp}")
                    state = _clear_and_reset(now)

        time.sleep(cfg.loop_sleep_secs)

    print("[SCALP_RUN_END] duration reached")


if __name__ == "__main__":
    monitor_live_scalp_reversal_v1()
