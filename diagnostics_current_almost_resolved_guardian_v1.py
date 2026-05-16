from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint
from typing import Optional

from market.book_5m import fetch_market_metadata_from_slug
from market.broker_types import BrokerOrderRequest
from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.polymarket_broker_v3 import PolymarketBrokerV3
from market.rest_5m_shadow_public_v5 import (
    _build_slot_bundle,
    _compute_executable_metrics,
    _fetch_slot_state,
    _slot_snapshot,
)
from market.rest_15m_shadow_public_v1 import build_slot_bundle_15m_v1
from market.slug_discovery import fetch_event_by_slug


@dataclass
class GuardianConfig:
    side: str
    entry_price: float
    qty: float
    price_stop: Optional[float]
    max_loss_ticks: int
    deadline_exit_secs: int
    warn_buffer_bps: float
    stop_buffer_bps: float
    stop_buffer_usd: float
    adverse_share_stop: float
    adverse_5s_stop_bps: float
    adverse_15s_stop_bps: float
    counter_stop_price: float
    counter_hard_stop_price: float
    market_range_30s_stop: float
    protective_grace_min_secs: int
    protective_grace_min_distance_bps: float
    protective_grace_min_distance_usd: float
    protective_grace_min_buffer_bps: float
    protective_grace_min_buffer_usd: float
    protective_grace_max_counter_price: float
    protective_grace_max_adverse_share: float
    hard_drawdown_ticks: int
    exit_slippage_ticks: int
    limit_first_secs: float
    exit_retry_secs: float
    min_market_order_qty: float
    execute_stop: bool
    beep: bool


@dataclass
class StopExitState:
    active: bool = False
    flat: bool = False
    token_id: str = ""
    order_id: Optional[str] = None
    first_triggered_at: float = 0.0
    last_attempt_at: float = 0.0
    attempts: int = 0
    limit_attempts: int = 0
    market_attempts: int = 0
    last_method: str = ""
    last_price: Optional[float] = None
    last_qty: float = 0.0
    last_reason: str = ""
    last_error: str = ""


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _slot_secs_to_end(item: dict | None) -> int | None:
    if not item:
        return None
    secs = _safe_float(item.get("seconds_to_end"), 0.0)
    if secs <= 0:
        return 0
    return int(secs)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_default_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"current_almost_resolved_guardian_{ts}.jsonl"


import threading as _threading

_TASKBAR_COLOR: dict = {"current": ""}
_alert_thread: object = None
_alert_stop_event = _threading.Event()


def _set_taskbar_color(color: str) -> None:
    """
    Set taskbar progress indicator color (Windows Terminal OSC 9;4) and window title.
    color: "green" | "yellow" | "red" | "clear"
    Only writes when color actually changes to avoid redundant I/O.
    """
    if _TASKBAR_COLOR.get("current") == color:
        return
    _TASKBAR_COLOR["current"] = color
    try:
        import sys
        # OSC 9;4 — Windows Terminal progress state: 0=off 1=green 2=red 3=yellow
        osc_state = {"green": "1", "yellow": "3", "red": "2", "clear": "0"}.get(color, "0")
        sys.stdout.write(f"\033]9;4;{osc_state}\007")
        sys.stdout.flush()
    except Exception:
        pass
    try:
        import ctypes
        titles = {
            "green":  "🟢 GUARDIAN — monitorando",
            "yellow": "🟡 GUARDIAN — ATENÇÃO",
            "red":    "🔴 GUARDIAN — STOP",
            "clear":  "GUARDIAN",
        }
        ctypes.windll.kernel32.SetConsoleTitleW(titles.get(color, "GUARDIAN"))
    except Exception:
        pass


def _flash_window(kind: str) -> None:
    """Flash the taskbar button without stealing focus (Windows only)."""
    try:
        import ctypes
        import ctypes.wintypes

        FLASHW_ALL       = 3
        FLASHW_TIMERNOFG = 12

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize",    ctypes.wintypes.UINT),
                ("hwnd",      ctypes.wintypes.HWND),
                ("dwFlags",   ctypes.wintypes.DWORD),
                ("uCount",    ctypes.wintypes.UINT),
                ("dwTimeout", ctypes.wintypes.DWORD),
            ]

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return
        fi = FLASHWINFO(cbSize=ctypes.sizeof(FLASHWINFO), hwnd=hwnd, dwTimeout=0)
        if kind in ("stop", "error"):
            fi.dwFlags = FLASHW_ALL | FLASHW_TIMERNOFG  # pisca até clicar
            fi.uCount  = 0
        elif kind == "warn":
            fi.dwFlags = FLASHW_ALL
            fi.uCount  = 3
        elif kind == "detect":
            fi.dwFlags = FLASHW_ALL
            fi.uCount  = 1
        else:
            return
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(fi))
    except Exception:
        return


def _start_continuous_alert() -> None:
    """Background thread: beeps repeatedly until the user focuses the guardian window."""
    global _alert_thread
    if _alert_thread is not None and _alert_thread.is_alive():
        return
    _alert_stop_event.clear()

    def _loop() -> None:
        try:
            import ctypes, winsound
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            while not _alert_stop_event.is_set():
                fg = ctypes.windll.user32.GetForegroundWindow()
                if fg == hwnd:
                    _alert_stop_event.set()
                    break
                winsound.Beep(1100, 280)
                _alert_stop_event.wait(1.4)
        except Exception:
            pass

    _alert_thread = _threading.Thread(target=_loop, daemon=True)
    _alert_thread.start()


def _beep(kind: str) -> None:
    try:
        import winsound
        if kind == "stop":
            winsound.Beep(1200, 300)
        elif kind == "detect":
            winsound.Beep(880, 150)
    except Exception:
        return


def _tick_size_from_snap(snap: dict, side: str) -> float:
    book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(book.get("tick_size"), 0.01))


def _token_id_for_side(snap: dict, side: str) -> str:
    book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return str(book.get("token_id") or "")


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


def _token_balance_qty(broker, token_id: Optional[str]) -> Optional[float]:
    if not token_id:
        return None
    try:
        payload = broker.get_balance_allowance(asset_type="CONDITIONAL", token_id=token_id)
        raw_balance = float(payload.get("balance") or 0.0)
        return round(raw_balance / 1_000_000.0, 6)
    except Exception:
        return None


def _is_flat_qty(qty: Optional[float], epsilon: float = 0.000001) -> bool:
    return qty is not None and abs(float(qty)) <= float(epsilon)


def _bid_for_side(executable: dict | None, side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _buy_for_side(signal: dict, side: str) -> float:
    return _safe_float(signal.get("up_buy" if side == "UP" else "down_buy"), 0.0)


def _counter_buy_for_side(signal: dict, side: str) -> float:
    return _safe_float(signal.get("down_buy" if side == "UP" else "up_buy"), 0.0)


def _guardian_ask_for_side(signal: dict, scalp_signal: dict, side: str) -> float:
    scalp_ask = _safe_float(scalp_signal.get("up_ask" if side == "UP" else "down_ask"), 0.0)
    if 0.0 < scalp_ask <= 1.0:
        return scalp_ask
    return _buy_for_side(signal, side)


def _guardian_counter_ask(signal: dict, scalp_signal: dict, side: str) -> float:
    counter_side = "DOWN" if side == "UP" else "UP"
    scalp_ask = _safe_float(scalp_signal.get("down_ask" if side == "UP" else "up_ask"), 0.0)
    if 0.0 < scalp_ask <= 1.0:
        return scalp_ask
    return _buy_for_side(signal, counter_side)


def _side_buffer_bps(signal: dict, side: str) -> float:
    return _safe_float(signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"), 0.0)


def _side_buffer_usd(signal: dict, side: str) -> float:
    return _safe_float(signal.get("up_price_to_beat_buffer_usd" if side == "UP" else "down_price_to_beat_buffer_usd"), 0.0)


def _side_adverse_bps(signal: dict, side: str) -> float:
    return _safe_float(signal.get("up_adverse_spot_bps" if side == "UP" else "down_adverse_spot_bps"), 0.0)


def _enrich_guardian_signal_metrics(signal: dict, scalp_signal: dict, side: str) -> dict:
    enriched = dict(signal)
    reference_price = _safe_float(scalp_signal.get("reference_price"), 0.0)
    opening_price = _safe_float(scalp_signal.get("opening_reference_price"), 0.0)
    distance_from_open_bps = _safe_float(scalp_signal.get("distance_from_open_bps"), 0.0)
    if reference_price <= 0 or opening_price <= 0:
        return enriched

    distance_bps = abs(distance_from_open_bps)
    distance_usd = abs(reference_price - opening_price)
    spot_delta_5s = _safe_float(scalp_signal.get("spot_delta_5s_bps"), 0.0)
    spot_delta_15s = _safe_float(scalp_signal.get("spot_delta_15s_bps"), 0.0)
    spot_delta_30s = _safe_float(scalp_signal.get("spot_delta_30s_bps"), 0.0)
    up_adverse_bps = max(0.0, -spot_delta_5s, -spot_delta_15s, -spot_delta_30s)
    down_adverse_bps = max(0.0, spot_delta_5s, spot_delta_15s, spot_delta_30s)
    up_winning = reference_price > opening_price
    down_winning = reference_price < opening_price

    def _fill(key: str, value: float) -> None:
        current = enriched.get(key)
        if current is None or _safe_float(current, 0.0) <= 0.0:
            enriched[key] = round(value, 6)

    _fill("distance_to_price_to_beat_bps", distance_bps)
    _fill("distance_to_price_to_beat_usd", distance_usd)
    _fill("up_adverse_spot_bps", up_adverse_bps)
    _fill("down_adverse_spot_bps", down_adverse_bps)
    _fill("up_adverse_spot_usd", reference_price * up_adverse_bps / 10000.0)
    _fill("down_adverse_spot_usd", reference_price * down_adverse_bps / 10000.0)

    up_buffer_bps = max(0.0, distance_bps - up_adverse_bps) if up_winning else 0.0
    down_buffer_bps = max(0.0, distance_bps - down_adverse_bps) if down_winning else 0.0
    up_buffer_usd = max(0.0, distance_usd - reference_price * up_adverse_bps / 10000.0) if up_winning else 0.0
    down_buffer_usd = max(0.0, distance_usd - reference_price * down_adverse_bps / 10000.0) if down_winning else 0.0
    if side == "UP":
        enriched["up_price_to_beat_buffer_bps"] = round(up_buffer_bps, 6)
        enriched["up_price_to_beat_buffer_usd"] = round(up_buffer_usd, 6)
    elif side == "DOWN":
        enriched["down_price_to_beat_buffer_bps"] = round(down_buffer_bps, 6)
        enriched["down_price_to_beat_buffer_usd"] = round(down_buffer_usd, 6)
    enriched["guardian_side_winning"] = bool((side == "UP" and up_winning) or (side == "DOWN" and down_winning))
    return enriched


def _current_slug_slot_bundle(slug: Optional[str], timeframe: str = "5m") -> dict:
    if not slug:
        if timeframe == "15m":
            return build_slot_bundle_15m_v1()
        return _build_slot_bundle()
    meta = fetch_market_metadata_from_slug(slug)
    event = fetch_event_by_slug(slug)
    if not meta or not event:
        return {"queue": {"current": None}, "slots": {"current": None}}
    item = {
        "title": event.get("title") or meta.get("event_title"),
        "slug": slug,
        "market_slug": meta.get("market_slug"),
        "seconds_to_end": None,
        "endDate": meta.get("endDate") or event.get("endDate"),
    }
    return {"queue": {"current": item}, "slots": {"current": {"item": item, "meta": meta}}}


def _event_open_reference(slug: str, cache: dict[str, object | None]) -> dict[str, object | None]:
    if slug and slug == cache.get("slug"):
        return cache
    raw_event = fetch_event_by_slug(slug)
    market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
    event_start_time = market.get("eventStartTime") or raw_event.get("startTime") if raw_event else None
    open_ref = fetch_binance_open_price_for_event_start_v1(event_start_time)
    cache.clear()
    cache.update(
        {
            "slug": slug,
            "price": open_ref.get("open_price"),
            "event_start_time": event_start_time,
            "open_reference": open_ref,
        }
    )
    return cache


def _price_to_beat_crossed(side: str, reference_price: float, opening_price: float) -> bool:
    if reference_price <= 0 or opening_price <= 0:
        return False
    if side == "UP":
        return reference_price <= opening_price
    return reference_price >= opening_price


def _protective_grace_active(
    *,
    cfg: GuardianConfig,
    signal: dict,
    scalp_signal: dict,
    side: str,
    secs_to_end: Optional[int],
) -> tuple[bool, list[str]]:
    buffer_bps = _side_buffer_bps(signal, side)
    buffer_usd = _side_buffer_usd(signal, side)
    distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    distance_usd = abs(_safe_float(signal.get("distance_to_price_to_beat_usd"), 0.0))
    adverse_bps = _side_adverse_bps(signal, side)
    adverse_share = adverse_bps / distance_bps if distance_bps > 0 else 999.0
    counter_price = _guardian_counter_ask(signal, scalp_signal, side)
    reasons: list[str] = []

    if not bool(signal.get("guardian_side_winning")):
        reasons.append("side_not_winning")
    if secs_to_end is None or secs_to_end < cfg.protective_grace_min_secs:
        reasons.append(f"not_enough_time secs={secs_to_end}")
    if distance_bps < cfg.protective_grace_min_distance_bps and distance_usd < cfg.protective_grace_min_distance_usd:
        reasons.append(f"distance_not_large bps={distance_bps} usd={distance_usd}")
    if buffer_bps < cfg.protective_grace_min_buffer_bps:
        reasons.append(f"buffer_bps_not_large buffer={buffer_bps}")
    if buffer_usd < cfg.protective_grace_min_buffer_usd:
        reasons.append(f"buffer_usd_not_large buffer={buffer_usd}")
    if counter_price > cfg.protective_grace_max_counter_price:
        reasons.append(f"counter_too_high counter={counter_price}")
    if adverse_share > cfg.protective_grace_max_adverse_share:
        reasons.append(f"adverse_share_too_high share={round(adverse_share, 4)}")

    return not reasons, reasons


def _decision(
    *,
    cfg: GuardianConfig,
    signal: dict,
    scalp_signal: dict,
    bid_now: float,
    tick_size: float,
    secs_to_end: Optional[int],
) -> tuple[str, list[str]]:
    side = cfg.side
    reasons: list[str] = []
    warnings: list[str] = []
    price_stop = cfg.price_stop
    if price_stop is None:
        price_stop = round(max(0.01, cfg.entry_price - cfg.max_loss_ticks * tick_size), 6)

    reference_price = _safe_float(scalp_signal.get("reference_price"), 0.0)
    opening_price = _safe_float(scalp_signal.get("opening_reference_price"), 0.0)
    buffer_bps = _side_buffer_bps(signal, side)
    buffer_usd = _side_buffer_usd(signal, side)
    distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    adverse_bps = _side_adverse_bps(signal, side)
    counter_price = _guardian_counter_ask(signal, scalp_signal, side)
    market_range_30s = _safe_float(signal.get("market_range_30s"), 0.0)
    scalp_setup = str(scalp_signal.get("setup") or "")
    scalp_side = str(scalp_signal.get("side") or "")
    protective_grace, protective_blockers = _protective_grace_active(
        cfg=cfg,
        signal=signal,
        scalp_signal=scalp_signal,
        side=side,
        secs_to_end=secs_to_end,
    )

    def _stop_or_warn(reason: str, *, hard: bool = False) -> None:
        if protective_grace and not hard:
            warnings.append(f"protected_drawdown {reason}")
        else:
            reasons.append(reason)

    if _price_to_beat_crossed(side, reference_price, opening_price):
        reasons.append(f"price_to_beat_crossed spot={reference_price} open={opening_price}")
    if bid_now > 0 and bid_now <= price_stop:
        hard_drawdown_price = round(max(0.01, cfg.entry_price - cfg.hard_drawdown_ticks * tick_size), 6)
        _stop_or_warn(
            f"price_stop bid={bid_now} stop={price_stop}",
            hard=bid_now <= hard_drawdown_price,
        )
    if buffer_bps <= cfg.stop_buffer_bps:
        reasons.append(f"buffer_bps_low buffer={buffer_bps} threshold={cfg.stop_buffer_bps}")
    if buffer_usd <= cfg.stop_buffer_usd:
        reasons.append(f"buffer_usd_low buffer={buffer_usd} threshold={cfg.stop_buffer_usd}")
    if distance_bps > 0 and adverse_bps >= max(cfg.adverse_5s_stop_bps, distance_bps * cfg.adverse_share_stop):
        _stop_or_warn(f"adverse_reversal_share adverse={adverse_bps} distance={distance_bps}")
    if adverse_bps >= cfg.adverse_15s_stop_bps and buffer_bps <= max(cfg.warn_buffer_bps, cfg.stop_buffer_bps * 2.0):
        _stop_or_warn(f"fast_adverse_reversal adverse={adverse_bps} buffer={buffer_bps}")
    if scalp_setup == "reversal" and scalp_side in ("UP", "DOWN") and scalp_side != side:
        _stop_or_warn(f"opposite_scalp_reversal scalp_side={scalp_side}")
    if counter_price >= cfg.counter_stop_price:
        _stop_or_warn(
            f"counter_pressure counter={counter_price} threshold={cfg.counter_stop_price}",
            hard=counter_price >= cfg.counter_hard_stop_price,
        )
    if market_range_30s >= cfg.market_range_30s_stop and adverse_bps >= cfg.adverse_5s_stop_bps:
        _stop_or_warn(f"market_range_with_adverse range30={market_range_30s} adverse={adverse_bps}")
    if secs_to_end is not None and secs_to_end <= cfg.deadline_exit_secs:
        reasons.append(f"deadline secs_to_end={secs_to_end}")

    if reasons:
        return "STOP", reasons

    if buffer_bps <= cfg.warn_buffer_bps:
        warnings.append(f"buffer_warning buffer={buffer_bps}")
    if protective_grace:
        warnings.append("protective_grace_active")
    elif protective_blockers:
        warnings.append("protective_grace_off " + ",".join(protective_blockers[:3]))
    if distance_bps > 0 and adverse_bps >= distance_bps * (cfg.adverse_share_stop * 0.65):
        warnings.append(f"adverse_consuming_distance adverse={adverse_bps} distance={distance_bps}")
    if counter_price >= cfg.counter_stop_price * 0.75:
        warnings.append(f"counter_warning counter={counter_price}")
    if scalp_setup == "reversal" and scalp_side == side:
        warnings.append("same_side_reversal_context")
    if warnings:
        return "WARN", warnings
    return "HOLD", []


def _limit_exit_price(bid_now: float, tick_size: float, slippage_ticks: int) -> float:
    if bid_now <= 0:
        return 0.01
    return round(max(0.01, bid_now - max(0, slippage_ticks) * tick_size), 6)


def _execute_or_simulate_stop_cycle(
    *,
    cfg: GuardianConfig,
    state: StopExitState,
    snap: dict,
    bid_now: float,
    tick_size: float,
    slug: str,
    reasons: list[str],
    now: float,
) -> dict:
    token_id = _token_id_for_side(snap, cfg.side)
    if not token_id:
        return {"ok": False, "reason": "missing_token_id"}
    if cfg.qty <= 0:
        return {"ok": False, "reason": "missing_qty"}
    if not state.active:
        state.active = True
        state.token_id = token_id
        state.first_triggered_at = now
        state.last_reason = ";".join(reasons)

    elapsed = max(0.0, now - state.first_triggered_at)
    should_use_market = elapsed >= cfg.limit_first_secs and hasattr(PolymarketBrokerV3, "place_market_order")
    method = "market_fak" if should_use_market else "limit_aggressive"
    qty = float(cfg.qty)
    exit_price = _limit_exit_price(bid_now, tick_size, cfg.exit_slippage_ticks)

    if not cfg.execute_stop:
        state.attempts += 1
        state.last_attempt_at = now
        state.last_method = method
        state.last_price = None if method == "market_fak" else exit_price
        state.last_qty = qty
        if method == "market_fak":
            state.market_attempts += 1
        else:
            state.limit_attempts += 1
        return {
            "ok": True,
            "simulated": True,
            "method": method,
            "qty": qty,
            "exit_price": exit_price,
            "state": asdict(state),
            "note": "simulation_only_add_--execute-stop_to_place_orders",
        }

    broker = PolymarketBrokerV3.from_env()
    health = broker.healthcheck().as_dict()
    if not health.get("ok"):
        state.last_error = "broker_healthcheck_failed"
        return {"ok": False, "reason": "broker_healthcheck_failed", "health": health, "state": asdict(state)}

    balance = _token_balance_qty(broker, token_id)
    if _is_flat_qty(balance):
        state.flat = True
        state.active = False
        state.last_reason = "flat"
        return {"ok": True, "flat": True, "token_balance_qty": balance, "state": asdict(state), "health": health}
    if balance is not None and balance > 0:
        qty = min(qty, float(balance))

    cancel_resp = None
    if state.order_id:
        cancel_resp = _cancel_if_live(broker, state.order_id)
        state.order_id = None

    try:
        if method == "market_fak" and qty >= cfg.min_market_order_qty and hasattr(broker, "place_market_order"):
            order = broker.place_market_order(
                token_id=token_id,
                side="SELL",
                amount=float(qty),
                order_type="FAK",
                market_slug=slug,
                outcome=cfg.side,
            )
            state.market_attempts += 1
            state.last_price = None
        else:
            req = BrokerOrderRequest(
                token_id=token_id,
                side="SELL",
                price=exit_price,
                size=float(qty),
                order_type="FAK" if qty < cfg.min_market_order_qty else "GTC",
                market_slug=slug,
                outcome=cfg.side,
                client_order_key=f"guardian_stop:{method}:{int(now)}:{cfg.side}",
            )
            order = broker.place_limit_order(req)
            state.limit_attempts += 1
            state.last_price = exit_price
            method = f"limit_{req.order_type.lower()}"
        state.attempts += 1
        state.last_attempt_at = now
        state.last_method = method
        state.last_qty = qty
        state.order_id = str(order.order_id or "")
        state.last_error = ""
        return {
            "ok": True,
            "flat": False,
            "method": method,
            "qty": qty,
            "exit_price": exit_price,
            "cancel_previous": cancel_resp,
            "token_balance_qty": balance,
            "order": order.as_dict(),
            "health": health,
            "state": asdict(state),
        }
    except Exception as exc:
        state.attempts += 1
        state.last_attempt_at = now
        state.last_method = method
        state.last_qty = qty
        state.last_error = f"{type(exc).__name__}: {exc}"
        return {
            "ok": False,
            "reason": "stop_order_failed",
            "method": method,
            "qty": qty,
            "exit_price": exit_price,
            "token_balance_qty": balance,
            "cancel_previous": cancel_resp,
            "error": state.last_error,
            "health": health,
            "state": asdict(state),
        }


def _slot_start_ts(timeframe: str) -> int:
    step = 900 if timeframe == "15m" else 300
    return (int(time.time()) // step) * step


def _fetch_trades_filtered(broker, token_id: str, after_ts: int) -> list:
    try:
        try:
            from py_clob_client_v2.clob_types import TradeParams
        except ImportError:
            from py_clob_client.clob_types import TradeParams
        params = TradeParams(asset_id=token_id, after=after_ts)
        result = broker.get_trades(params)
        return result if isinstance(result, list) else []
    except Exception:
        return []


def _vwap_from_trade_list(trades: list, token_id: str) -> Optional[float]:
    notional = 0.0
    qty = 0.0
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        side = str(t.get("side") or t.get("taker_side") or t.get("maker_side") or "").upper()
        if side and side not in ("BUY", ""):
            continue
        asset = str(t.get("asset_id") or t.get("token_id") or t.get("asset") or "")
        if asset and asset != token_id and token_id not in str(t):
            continue
        trade_qty = 0.0
        for key in ("size", "size_matched", "matched_size", "filled_size", "qty", "quantity"):
            trade_qty = _safe_float(t.get(key), 0.0)
            if trade_qty > 0:
                break
        price = _safe_float(t.get("price"), 0.0)
        if trade_qty > 0 and price > 0:
            notional += trade_qty * price
            qty += trade_qty
    if qty <= 0:
        return None
    return round(notional / qty, 6)


def _detect_open_position(broker, snap: dict, slot_start_ts: int) -> Optional[dict]:
    best = None
    for side in ("UP", "DOWN"):
        token_id = _token_id_for_side(snap, side)
        if not token_id:
            continue
        qty = _token_balance_qty(broker, token_id)
        if not qty or qty < 0.001:
            continue
        if best is None or qty > best["qty"]:
            trades = _fetch_trades_filtered(broker, token_id, slot_start_ts)
            entry_price = _vwap_from_trade_list(trades, token_id)
            if not entry_price or entry_price <= 0:
                book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
                entry_price = _safe_float(book.get("best_ask") or book.get("best_bid"), 0.0)
            best = {
                "side": side,
                "qty": round(qty, 4),
                "entry_price": round(entry_price, 6) if entry_price else 0.0,
                "token_id": token_id,
                "trades_used": len(trades),
            }
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description="Background guardian for manual current almost-resolved positions")
    parser.add_argument("--side", default=None, choices=["UP", "DOWN"], help="Position side. Omit for auto-detect.")
    parser.add_argument("--entry-price", type=float, default=None, help="Average entry price. Omit for auto-detect from trade history.")
    parser.add_argument("--qty", type=float, default=None, help="Contracts to sell on stop. Omit for auto-detect from token balance.")
    parser.add_argument("--slug", type=str, default=None, help="Optional event slug. Defaults to current 5m slot.")
    parser.add_argument("--seconds", type=int, default=0, help="Run duration. Use 0 to run indefinitely.")
    parser.add_argument("--poll-secs", type=float, default=1.0)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument("--price-stop", type=float, default=None, help="Explicit token bid stop. Defaults to entry - max-loss-ticks.")
    parser.add_argument("--max-loss-ticks", type=int, default=3)
    parser.add_argument("--deadline-exit-secs", type=int, default=8)
    parser.add_argument("--warn-buffer-bps", type=float, default=4.0)
    parser.add_argument("--stop-buffer-bps", type=float, default=1.2)
    parser.add_argument("--stop-buffer-usd", type=float, default=8.0)
    parser.add_argument("--adverse-share-stop", type=float, default=0.55)
    parser.add_argument("--adverse-5s-stop-bps", type=float, default=1.2)
    parser.add_argument("--adverse-15s-stop-bps", type=float, default=1.8)
    parser.add_argument("--counter-stop-price", type=float, default=0.12)
    parser.add_argument("--counter-hard-stop-price", type=float, default=0.35)
    parser.add_argument("--market-range-30s-stop", type=float, default=0.035)
    parser.add_argument("--protective-grace-min-secs", type=int, default=25)
    parser.add_argument("--protective-grace-min-distance-bps", type=float, default=6.0)
    parser.add_argument("--protective-grace-min-distance-usd", type=float, default=45.0)
    parser.add_argument("--protective-grace-min-buffer-bps", type=float, default=4.0)
    parser.add_argument("--protective-grace-min-buffer-usd", type=float, default=30.0)
    parser.add_argument("--protective-grace-max-counter-price", type=float, default=0.18)
    parser.add_argument("--protective-grace-max-adverse-share", type=float, default=0.45)
    parser.add_argument("--hard-drawdown-ticks", type=int, default=20)
    parser.add_argument("--exit-slippage-ticks", type=int, default=2)
    parser.add_argument("--limit-first-secs", type=float, default=2.0, help="Seconds to give the first aggressive limit exit before market FAK attempts")
    parser.add_argument("--exit-retry-secs", type=float, default=1.0, help="Retry cadence while a stop exit is still not flat")
    parser.add_argument("--min-market-order-qty", type=float, default=5.0, help="Minimum qty for market FAK; smaller residuals use FAK limit")
    parser.add_argument("--execute-stop", action="store_true", help="Actually post/cancel/retry SELL stop orders when STOP triggers")
    parser.add_argument("--no-beep", action="store_true")
    parser.add_argument(
        "--timeframe",
        type=str,
        default="5m",
        choices=["5m", "15m"],
        help="Market timeframe to monitor. 5m = BTC 5-min slot (default); 15m = BTC 15-min slot.",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="Path to .env file for broker credentials (default: .env). Use to trade a different account.",
    )
    args = parser.parse_args()

    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=True)
        print(f"[GUARDIAN_ENV] Loaded credentials from {args.env_file}")

    # Auto-detect mode: scan for open position when side/entry/qty are not given
    auto_detect = args.side is None or args.entry_price is None or args.qty is None
    if auto_detect:
        print()
        print("[GUARDIAN] MODO AUTO-DETECT — aguardando posição aberta...")
        print(f"[GUARDIAN] Timeframe: {args.timeframe} | Poll: {args.poll_secs}s")
        print("[GUARDIAN] Pressione Ctrl+C para cancelar.")
        print()
        broker_scan = PolymarketBrokerV3.from_env()
        detected = None
        while detected is None:
            try:
                slot_bundle = _current_slug_slot_bundle(args.slug, args.timeframe)
                current_item = slot_bundle["queue"].get("current")
                if current_item:
                    slot_state = _fetch_slot_state(slot_bundle)
                    snap = _slot_snapshot(slot_state, "current")
                    slot_start_ts = _slot_start_ts(args.timeframe)
                    detected = _detect_open_position(broker_scan, snap, slot_start_ts)
                    if detected:
                        print(
                            f"[GUARDIAN_DETECT] Posição encontrada: side={detected['side']} "
                            f"entry={detected['entry_price']} qty={detected['qty']} "
                            f"trades_usados={detected['trades_used']}"
                        )
                        _set_taskbar_color("green")
                        _flash_window("detect")
                        _beep("detect")
                        args.side = detected["side"]
                        if args.entry_price is None:
                            args.entry_price = detected["entry_price"]
                        if args.qty is None:
                            args.qty = detected["qty"]
                    else:
                        slug_now = str(current_item.get("slug") or "")
                        secs = current_item.get("seconds_to_end")
                        print(f"[GUARDIAN_SCAN] Sem posição aberta | {slug_now} | {secs}s restantes")
                else:
                    print("[GUARDIAN_SCAN] Slot indisponível — aguardando...")
            except Exception as exc:
                print(f"[GUARDIAN_SCAN_ERROR] {type(exc).__name__}: {exc}")
            if detected is None:
                time.sleep(max(0.25, float(args.poll_secs)))

    if not args.side:
        raise SystemExit("[GUARDIAN] Erro: --side não definido e auto-detect não encontrou posição.")
    if args.entry_price is None or args.entry_price <= 0:
        raise SystemExit("[GUARDIAN] Erro: --entry-price não definido e auto-detect não conseguiu calcular preço de entrada.")
    if args.qty is None:
        args.qty = 0.0

    cfg = GuardianConfig(
        side=args.side,
        entry_price=float(args.entry_price),
        qty=float(args.qty),
        price_stop=args.price_stop,
        max_loss_ticks=int(args.max_loss_ticks),
        deadline_exit_secs=int(args.deadline_exit_secs),
        warn_buffer_bps=float(args.warn_buffer_bps),
        stop_buffer_bps=float(args.stop_buffer_bps),
        stop_buffer_usd=float(args.stop_buffer_usd),
        adverse_share_stop=float(args.adverse_share_stop),
        adverse_5s_stop_bps=float(args.adverse_5s_stop_bps),
        adverse_15s_stop_bps=float(args.adverse_15s_stop_bps),
        counter_stop_price=float(args.counter_stop_price),
        counter_hard_stop_price=float(args.counter_hard_stop_price),
        market_range_30s_stop=float(args.market_range_30s_stop),
        protective_grace_min_secs=int(args.protective_grace_min_secs),
        protective_grace_min_distance_bps=float(args.protective_grace_min_distance_bps),
        protective_grace_min_distance_usd=float(args.protective_grace_min_distance_usd),
        protective_grace_min_buffer_bps=float(args.protective_grace_min_buffer_bps),
        protective_grace_min_buffer_usd=float(args.protective_grace_min_buffer_usd),
        protective_grace_max_counter_price=float(args.protective_grace_max_counter_price),
        protective_grace_max_adverse_share=float(args.protective_grace_max_adverse_share),
        hard_drawdown_ticks=int(args.hard_drawdown_ticks),
        exit_slippage_ticks=int(args.exit_slippage_ticks),
        limit_first_secs=float(args.limit_first_secs),
        exit_retry_secs=float(args.exit_retry_secs),
        min_market_order_qty=float(args.min_market_order_qty),
        execute_stop=bool(args.execute_stop),
        beep=not bool(args.no_beep),
    )
    if cfg.execute_stop and cfg.qty <= 0:
        raise SystemExit("--qty deve ser > 0 com --execute-stop (não foi detectado automaticamente ou passou 0)")

    signal_cfg = CurrentAlmostResolvedConfigV1()
    scalp_cfg = CurrentScalpConfigV1()
    current_scalp = CurrentScalpResearchV1(cfg=scalp_cfg, history_secs=180)
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    open_reference_cache: dict[str, object | None] = {}
    exit_state = StopExitState()

    print("[CURRENT_ALMOST_RESOLVED_GUARDIAN_CONFIG]")
    pprint(asdict(cfg))
    print("[LOG_FILE]")
    print(log_path)

    started_at = time.time()
    while args.seconds <= 0 or time.time() - started_at < args.seconds:
        now = time.time()
        row: dict = {"type": "snapshot", "ts": now, "guardian": asdict(cfg)}
        try:
            slot_bundle = _current_slug_slot_bundle(args.slug, args.timeframe)
            current_item = slot_bundle["queue"].get("current")
            if not current_item:
                row.update({"status": "WARN", "reasons": ["current_slot_unavailable"]})
                _append_jsonl(log_path, row)
                print("[GUARDIAN_WARN] current_slot_unavailable")
                time.sleep(max(0.25, float(args.poll_secs)))
                continue

            slug = str(current_item.get("slug") or "")
            open_ref = _event_open_reference(slug, open_reference_cache)
            slot_state = _fetch_slot_state(slot_bundle)
            snap = _slot_snapshot(slot_state, "current")
            executable, executable_reason = _compute_executable_metrics(snap)
            secs_to_end = _slot_secs_to_end(current_item)
            reference = fetch_external_btc_reference_v1()
            scalp_signal = current_scalp.evaluate(
                snap=snap,
                secs_to_end=secs_to_end,
                event_start_time=open_ref.get("event_start_time"),
                now_ts=now,
                reference_price=reference.get("reference_price"),
                source_divergence_bps=reference.get("source_divergence_bps"),
                opening_reference_price=open_ref.get("price"),
            )
            signal = evaluate_current_almost_resolved_v1(
                snap=snap,
                secs_to_end=secs_to_end,
                reference_signal=scalp_signal,
                cfg=signal_cfg,
            )
            signal = _enrich_guardian_signal_metrics(signal, scalp_signal, cfg.side)
            tick_size = _tick_size_from_snap(snap, cfg.side)
            bid_now = _bid_for_side(executable, cfg.side)
            decision, reasons = _decision(
                cfg=cfg,
                signal=signal,
                scalp_signal=scalp_signal,
                bid_now=bid_now,
                tick_size=tick_size,
                secs_to_end=secs_to_end,
            )
            pnl_ticks = round((bid_now - cfg.entry_price) / tick_size, 4) if bid_now > 0 and tick_size > 0 else None
            row.update(
                {
                    "status": decision,
                    "reasons": reasons,
                    "slug": slug,
                    "secs_to_end": secs_to_end,
                    "executable_reason": executable_reason,
                    "bid_now": bid_now,
                    "leader_buy": _guardian_ask_for_side(signal, scalp_signal, cfg.side),
                    "counter_buy": _guardian_counter_ask(signal, scalp_signal, cfg.side),
                    "tick_size": tick_size,
                    "pnl_ticks": pnl_ticks,
                    "reference": reference,
                    "open_reference": open_ref,
                    "current_scalp_context": scalp_signal,
                    "almost_resolved_signal": signal,
                }
            )
            print(
                f"[GUARDIAN_{decision}] side={cfg.side} secs={secs_to_end} bid={bid_now} "
                f"pnl_ticks={pnl_ticks} spot={scalp_signal.get('reference_price')} "
                f"open={scalp_signal.get('opening_reference_price')} "
                f"buffer_bps={_side_buffer_bps(signal, cfg.side)} "
                f"adverse_bps={_side_adverse_bps(signal, cfg.side)} "
                f"counter={_guardian_counter_ask(signal, scalp_signal, cfg.side)} reasons={';'.join(reasons) or '-'}"
            )
            if exit_state.active and decision != "STOP":
                decision = "STOP"
                reasons = ["exit_already_active"]
                row["status"] = decision
                row["reasons"] = reasons

            if decision == "HOLD":
                _set_taskbar_color("green")
            elif decision == "WARN":
                _set_taskbar_color("yellow")
                _flash_window("warn")

            if decision == "STOP":
                _set_taskbar_color("red")
                _flash_window("stop")
                if cfg.beep:
                    _beep("stop")
                if (not exit_state.active) or now - exit_state.last_attempt_at >= cfg.exit_retry_secs:
                    stop_resp = _execute_or_simulate_stop_cycle(
                        cfg=cfg,
                        state=exit_state,
                        snap=snap,
                        bid_now=bid_now,
                        tick_size=tick_size,
                        slug=slug,
                        reasons=reasons,
                        now=now,
                    )
                    row["stop_execution"] = stop_resp
                    print("[GUARDIAN_STOP_EXIT]")
                    pprint(stop_resp)
                else:
                    row["stop_execution"] = {
                        "ok": True,
                        "skipped": "waiting_retry_cadence",
                        "state": asdict(exit_state),
                    }
                if exit_state.flat:
                    _append_jsonl(log_path, row)
                    break
            _append_jsonl(log_path, row)
        except Exception as exc:
            row.update({"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"})
            _append_jsonl(log_path, row)
            print(f"[GUARDIAN_ERROR] {type(exc).__name__}: {exc}")
            _set_taskbar_color("red")
            _flash_window("error")
            _start_continuous_alert()
        time.sleep(max(0.25, float(args.poll_secs)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
