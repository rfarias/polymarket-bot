from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint
from typing import Optional

from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.next1_scalp_signal_v1 import Next1ScalpConfigV1, Next1ScalpResearchV1
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _fetch_slot_state, _slot_snapshot
from market.slug_discovery import fetch_event_by_slug


@dataclass
class ShadowExecConfig:
    entry_touch_polls: int = 2
    entry_min_age_secs: float = 1.0
    exit_touch_polls: int = 1
    exit_min_age_secs: float = 0.5
    reprice_entry_secs: float = 2.0
    reprice_exit_secs: float = 1.0
    force_exit_slippage_ticks: int = 1
    current_entry_timeout_secs: float = 10.0
    next1_entry_timeout_secs: float = 8.0
    almost_resolved_entry_timeout_secs: float = 2.5

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ShadowTrade:
    setup_key: str
    mode: str = "idle"  # idle | pending_entry | open | pending_exit
    side: Optional[str] = None
    event_slug: Optional[str] = None
    setup: Optional[str] = None
    setup_variant: Optional[str] = None
    entry_limit_price: Optional[float] = None
    entry_fill_price: Optional[float] = None
    entry_signal_price: Optional[float] = None
    exit_limit_price: Optional[float] = None
    exit_fill_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    best_bid: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    favorable_polls: int = 0
    reprices: int = 0
    last_reprice_at: float = 0.0
    exit_reason: Optional[str] = None
    pnl_ticks: Optional[float] = None
    hold_to_resolution: bool = False
    entry_buffer_bps: Optional[float] = None
    entry_distance_bps: Optional[float] = None


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _default_log_dir() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"directional_shadow_runner_{ts}"


def _tick_size_from_snap(snap: dict, side: str) -> float:
    book_side = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(book_side.get("tick_size"), 0.01))


def _bid_for_side(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _ask_for_side(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_ask" if side == "UP" else "down_ask"), 0.0)


def _slot_secs_to_end(item: Optional[dict]) -> Optional[int]:
    if not item:
        return None
    return max(0, int(_safe_float(item.get("seconds_to_end"), 0.0)))


def _trade_reset(setup_key: str) -> ShadowTrade:
    return ShadowTrade(setup_key=setup_key)


def _trade_closed(trade: ShadowTrade, *, exit_price: float, tick_size: float, reason: str, now: float) -> ShadowTrade:
    trade.mode = "idle"
    trade.exit_fill_price = round(max(0.01, float(exit_price)), 6)
    trade.exit_reason = str(reason)
    trade.updated_at = now
    if trade.entry_fill_price is not None and tick_size > 0:
        trade.pnl_ticks = round((trade.exit_fill_price - float(trade.entry_fill_price)) / tick_size, 4)
    return trade


def _current_signal_summary(signal: dict) -> dict:
    return {
        "setup": signal.get("setup"),
        "allow": signal.get("allow"),
        "side": signal.get("side"),
        "reason": signal.get("reason"),
        "entry_price": signal.get("entry_price"),
        "reference_price": signal.get("reference_price"),
    }


def _next1_signal_summary(signal: dict) -> dict:
    return {
        "setup": signal.get("setup"),
        "allow": signal.get("allow"),
        "side": signal.get("side"),
        "reason": signal.get("reason"),
        "entry_price": signal.get("entry_price"),
        "aggressive_entry_price": signal.get("aggressive_entry_price"),
        "current_secs": signal.get("current_secs"),
        "next1_secs": signal.get("next1_secs"),
    }


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


def _almost_resolved_exit_reason(
    trade: ShadowTrade,
    *,
    signal: dict,
    bid_now: float,
    tick_size: float,
    now: float,
    secs_to_end: Optional[int],
    cfg: CurrentAlmostResolvedConfigV1,
) -> Optional[str]:
    if tick_size <= 0 or trade.entry_fill_price is None:
        return None
    pnl_ticks_now = (bid_now - float(trade.entry_fill_price)) / tick_size
    side = trade.side or "UP"
    buffer_bps = _safe_float(signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"), 0.0)
    open_distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    market_range_30s = _safe_float(signal.get("market_range_30s"), 0.0)
    market_range_15s = _safe_float(signal.get("market_range_15s"), 0.0)
    edge_vs_counter = _safe_float(signal.get("up_edge_vs_counter" if side == "UP" else "down_edge_vs_counter"), 0.0)
    adverse_spot_bps = _safe_float(signal.get("up_adverse_spot_bps" if side == "UP" else "down_adverse_spot_bps"), 0.0)
    setup_variant = str(trade.setup_variant or signal.get("setup_variant") or "standard")
    entry_buffer_bps = _safe_float(trade.entry_buffer_bps, buffer_bps)
    entry_distance_bps = _safe_float(trade.entry_distance_bps, open_distance_bps)

    if _should_hold_to_resolution(signal, bid_now=bid_now, secs_to_end=secs_to_end, cfg=cfg, side=side):
        trade.hold_to_resolution = True

    if (
        setup_variant == "controlled_late_entry"
        and pnl_ticks_now > 0
        and (
            market_range_15s >= cfg.controlled_late_max_market_range_15s
            or adverse_spot_bps >= cfg.controlled_late_max_adverse_spot_15s_bps
            or buffer_bps <= max(cfg.paper_structural_stop_buffer_bps, entry_buffer_bps * 0.7)
            or open_distance_bps <= max(cfg.min_price_to_beat_distance_bps, entry_distance_bps * 0.7)
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
    if secs_to_end is not None and secs_to_end <= cfg.min_secs_to_end:
        return "deadline"
    if not trade.hold_to_resolution and now - trade.created_at >= cfg.max_hold_secs:
        return "timeout"
    return None


def _create_trade(
    setup_key: str,
    *,
    signal: dict,
    event_slug: Optional[str],
    tick_size: float,
    target_ticks: int,
    stop_ticks: int,
    now: float,
) -> ShadowTrade:
    entry_signal_price = _safe_float(signal.get("entry_price"), 0.0)
    entry_limit_price = _safe_float(signal.get("aggressive_entry_price") or signal.get("entry_price"), 0.0)
    trade = ShadowTrade(
        setup_key=setup_key,
        mode="pending_entry",
        side=str(signal.get("side") or ""),
        event_slug=event_slug,
        setup=str(signal.get("setup") or setup_key),
        setup_variant=str(signal.get("setup_variant") or "standard"),
        entry_limit_price=round(entry_limit_price, 6),
        entry_signal_price=round(entry_signal_price, 6),
        target_price=round(min(0.99, entry_signal_price + target_ticks * tick_size), 6),
        stop_price=round(max(0.01, entry_signal_price - stop_ticks * tick_size), 6),
        created_at=now,
        updated_at=now,
        last_reprice_at=now,
        entry_buffer_bps=_safe_float(
            signal.get("up_price_to_beat_buffer_bps" if signal.get("side") == "UP" else "down_price_to_beat_buffer_bps"),
            0.0,
        ),
        entry_distance_bps=abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0)),
    )
    explicit_exit = _safe_float(signal.get("exit_price"), 0.0)
    if explicit_exit > 0:
        trade.target_price = round(min(0.99, explicit_exit), 6)
    return trade


def _entry_timeout_secs(setup_key: str, exec_cfg: ShadowExecConfig) -> float:
    if setup_key == "next1_scalp":
        return exec_cfg.next1_entry_timeout_secs
    if setup_key == "current_almost_resolved":
        return exec_cfg.almost_resolved_entry_timeout_secs
    return exec_cfg.current_entry_timeout_secs


def _desired_entry_price(trade: ShadowTrade, signal: dict) -> float:
    if trade.setup_key == "next1_scalp":
        return _safe_float(signal.get("aggressive_entry_price") or signal.get("entry_price"), 0.0)
    return _safe_float(signal.get("entry_price"), 0.0)


def _entry_still_valid(trade: ShadowTrade, signal: dict, secs_to_end: Optional[int]) -> bool:
    if not signal.get("allow"):
        return False
    if signal.get("side") != trade.side:
        return False
    if trade.setup_key == "next1_scalp":
        next1_secs = signal.get("next1_secs")
        if next1_secs is not None and int(next1_secs) < 330:
            return False
    if trade.setup_key == "current_almost_resolved" and secs_to_end is not None and secs_to_end <= 15:
        return False
    return True


def _manage_entry(
    trade: ShadowTrade,
    *,
    signal: dict,
    executable: Optional[dict],
    tick_size: float,
    now: float,
    secs_to_end: Optional[int],
    exec_cfg: ShadowExecConfig,
) -> tuple[ShadowTrade, Optional[str]]:
    ask_now = _ask_for_side(executable, trade.side or "UP")
    if not _entry_still_valid(trade, signal, secs_to_end):
        return _trade_reset(trade.setup_key), "entry_signal_invalidated"

    if ask_now > 0 and ask_now <= _safe_float(trade.entry_limit_price):
        trade.favorable_polls += 1
    else:
        trade.favorable_polls = 0

    age = now - trade.created_at
    if (
        ask_now > 0
        and trade.favorable_polls >= exec_cfg.entry_touch_polls
        and age >= exec_cfg.entry_min_age_secs
    ):
        trade.mode = "open"
        trade.entry_fill_price = round(ask_now, 6)
        trade.updated_at = now
        trade.favorable_polls = 0
        trade.stop_price = round(max(0.01, float(trade.entry_fill_price) - (_safe_float(trade.entry_signal_price) - _safe_float(trade.stop_price))), 6)
        target_delta = _safe_float(trade.target_price) - _safe_float(trade.entry_signal_price)
        trade.target_price = round(min(0.99, float(trade.entry_fill_price) + target_delta), 6)
        stop_delta = _safe_float(trade.entry_signal_price) - _safe_float(trade.stop_price)
        trade.stop_price = round(max(0.01, float(trade.entry_fill_price) - stop_delta), 6)
        return trade, "entry_filled"

    desired_price = _desired_entry_price(trade, signal)
    if (
        desired_price > 0
        and desired_price != _safe_float(trade.entry_limit_price)
        and now - trade.last_reprice_at >= exec_cfg.reprice_entry_secs
    ):
        trade.entry_limit_price = round(desired_price, 6)
        trade.last_reprice_at = now
        trade.updated_at = now
        trade.reprices += 1
        trade.favorable_polls = 0
        return trade, "entry_repriced"

    if age >= _entry_timeout_secs(trade.setup_key, exec_cfg):
        return _trade_reset(trade.setup_key), "entry_timeout"
    return trade, None


def _manage_open_current_scalp(
    trade: ShadowTrade,
    *,
    bid_now: float,
    tick_size: float,
    now: float,
    secs_to_end: Optional[int],
    cfg: CurrentScalpConfigV1,
) -> Optional[str]:
    trade.best_bid = max(_safe_float(trade.best_bid, 0.0), bid_now)
    if trade.best_bid >= round(_safe_float(trade.entry_fill_price) + tick_size, 6):
        trade.stop_price = max(_safe_float(trade.stop_price), round(trade.best_bid - tick_size, 6))
    if bid_now >= _safe_float(trade.target_price):
        return "target"
    if bid_now <= _safe_float(trade.stop_price):
        return "stop"
    if secs_to_end is not None and secs_to_end <= 5:
        return "deadline"
    if now - trade.created_at >= cfg.max_hold_secs:
        return "timeout"
    return None


def _manage_open_next1_scalp(
    trade: ShadowTrade,
    *,
    bid_now: float,
    tick_size: float,
    now: float,
    secs_to_end: Optional[int],
    cfg: Next1ScalpConfigV1,
) -> Optional[str]:
    trade.best_bid = max(_safe_float(trade.best_bid, 0.0), bid_now)
    if trade.best_bid >= round(_safe_float(trade.entry_fill_price) + tick_size, 6):
        trade.stop_price = max(_safe_float(trade.stop_price), round(trade.best_bid - tick_size, 6))
    if bid_now >= _safe_float(trade.target_price):
        return "target"
    if bid_now <= _safe_float(trade.stop_price):
        return "stop"
    if secs_to_end is not None and secs_to_end <= cfg.min_secs_to_end:
        return "deadline"
    if now - trade.created_at >= cfg.max_hold_secs:
        return "timeout"
    return None


def _manage_open(
    trade: ShadowTrade,
    *,
    signal: dict,
    executable: Optional[dict],
    tick_size: float,
    now: float,
    secs_to_end: Optional[int],
    current_scalp_cfg: CurrentScalpConfigV1,
    next1_cfg: Next1ScalpConfigV1,
    almost_cfg: CurrentAlmostResolvedConfigV1,
) -> tuple[ShadowTrade, Optional[str]]:
    bid_now = _bid_for_side(executable, trade.side or "UP")
    if bid_now <= 0:
        return trade, None
    if trade.setup_key == "current_scalp":
        reason = _manage_open_current_scalp(trade, bid_now=bid_now, tick_size=tick_size, now=now, secs_to_end=secs_to_end, cfg=current_scalp_cfg)
    elif trade.setup_key == "next1_scalp":
        reason = _manage_open_next1_scalp(trade, bid_now=bid_now, tick_size=tick_size, now=now, secs_to_end=secs_to_end, cfg=next1_cfg)
    else:
        reason = _almost_resolved_exit_reason(
            trade,
            signal=signal,
            bid_now=bid_now,
            tick_size=tick_size,
            now=now,
            secs_to_end=secs_to_end,
            cfg=almost_cfg,
        )
    if reason:
        trade.mode = "pending_exit"
        trade.exit_limit_price = round(bid_now, 6)
        trade.exit_reason = reason
        trade.updated_at = now
        trade.favorable_polls = 0
        return trade, "exit_posted"
    return trade, None


def _manage_exit(
    trade: ShadowTrade,
    *,
    executable: Optional[dict],
    tick_size: float,
    now: float,
    secs_to_end: Optional[int],
    exec_cfg: ShadowExecConfig,
) -> tuple[ShadowTrade, Optional[str]]:
    bid_now = _bid_for_side(executable, trade.side or "UP")
    if bid_now > 0 and bid_now >= _safe_float(trade.exit_limit_price):
        trade.favorable_polls += 1
    else:
        trade.favorable_polls = 0

    age = now - trade.updated_at
    if bid_now > 0 and trade.favorable_polls >= exec_cfg.exit_touch_polls and age >= exec_cfg.exit_min_age_secs:
        return _trade_closed(trade, exit_price=bid_now, tick_size=tick_size, reason=trade.exit_reason or "exit", now=now), "exit_filled"

    if secs_to_end is not None and secs_to_end <= 2 and bid_now > 0:
        force_price = round(max(0.01, bid_now - (exec_cfg.force_exit_slippage_ticks * tick_size)), 6)
        return _trade_closed(trade, exit_price=force_price, tick_size=tick_size, reason=f"{trade.exit_reason or 'exit'}_force", now=now), "exit_forced"

    if bid_now > 0 and now - trade.last_reprice_at >= exec_cfg.reprice_exit_secs and bid_now != _safe_float(trade.exit_limit_price):
        trade.exit_limit_price = round(bid_now, 6)
        trade.last_reprice_at = now
        trade.updated_at = now
        trade.reprices += 1
        trade.favorable_polls = 0
        return trade, "exit_repriced"
    return trade, None


def _trade_stats(completed: list[dict]) -> dict:
    by_setup: dict[str, dict] = {}
    for trade in completed:
        key = str(trade.get("setup_key") or "unknown")
        bucket = by_setup.setdefault(
            key,
            {"count": 0, "wins": 0, "losses": 0, "flat": 0, "total_pnl_ticks": 0.0},
        )
        pnl = _safe_float(trade.get("pnl_ticks"))
        bucket["count"] += 1
        bucket["total_pnl_ticks"] = round(bucket["total_pnl_ticks"] + pnl, 4)
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
        else:
            bucket["flat"] += 1
    return by_setup


def main() -> int:
    parser = argparse.ArgumentParser(description="Directional shadow runner with runner-like execution friction")
    parser.add_argument("--seconds", type=int, default=300, help="Run duration")
    parser.add_argument("--poll-secs", type=float, default=1.0, help="Polling interval")
    parser.add_argument("--log-dir", type=str, default=None, help="Optional session log directory")
    args = parser.parse_args()

    session_dir = Path(args.log_dir) if args.log_dir else _default_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "directional_shadow_runner.jsonl"

    next1_cfg = Next1ScalpConfigV1()
    current_scalp_cfg = CurrentScalpConfigV1()
    almost_cfg = CurrentAlmostResolvedConfigV1()
    exec_cfg = ShadowExecConfig()
    next1_research = Next1ScalpResearchV1(cfg=next1_cfg)
    current_research = CurrentScalpResearchV1(cfg=current_scalp_cfg)

    print("[NEXT1_SCALP_CONFIG]")
    pprint(next1_cfg.as_dict())
    print("[CURRENT_SCALP_CONFIG]")
    pprint(current_scalp_cfg.as_dict())
    print("[CURRENT_ALMOST_RESOLVED_CONFIG]")
    pprint(almost_cfg.as_dict())
    print("[SHADOW_EXEC_CONFIG]")
    pprint(exec_cfg.as_dict())
    print("[LOG_DIR]")
    print(session_dir)

    completed: list[dict] = []
    blocked = Counter()
    current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}
    trades = {
        "current_scalp": _trade_reset("current_scalp"),
        "current_almost_resolved": _trade_reset("current_almost_resolved"),
        "next1_scalp": _trade_reset("next1_scalp"),
    }

    started_at = time.time()
    while time.time() - started_at < args.seconds:
        now = time.time()
        slot_bundle = _build_slot_bundle()
        slot_state = _fetch_slot_state(slot_bundle)
        current_item = slot_bundle["queue"].get("current")
        next1_item = slot_bundle["queue"].get("next_1")
        current_secs = _slot_secs_to_end(current_item)
        next1_secs = _slot_secs_to_end(next1_item)
        current_snap = _slot_snapshot(slot_state, "current")
        next1_snap = _slot_snapshot(slot_state, "next_1")
        current_exec, _ = _compute_executable_metrics(current_snap)
        next1_exec, _ = _compute_executable_metrics(next1_snap)

        if current_item and current_item["slug"] != current_open_reference.get("slug"):
            raw_event = fetch_event_by_slug(current_item["slug"])
            market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
            start_time = market.get("eventStartTime") or raw_event.get("startTime") if raw_event else None
            opened = fetch_binance_open_price_for_event_start_v1(start_time) if start_time else {"open_price": None}
            current_open_reference = {"slug": current_item["slug"], "price": opened.get("open_price"), "event_start_time": start_time}

        ref = fetch_external_btc_reference_v1()
        current_signal = current_research.evaluate(
            snap=current_snap,
            secs_to_end=current_secs,
            event_start_time=current_open_reference.get("event_start_time"),
            now_ts=now,
            reference_price=ref.get("reference_price"),
            source_divergence_bps=ref.get("source_divergence_bps"),
            opening_reference_price=current_open_reference.get("price"),
        ) if current_item else {"allow": False, "reason": "missing_current"}
        almost_signal = evaluate_current_almost_resolved_v1(
            snap=current_snap,
            secs_to_end=current_secs,
            reference_signal=current_signal,
            cfg=almost_cfg,
        ) if current_item else {"allow": False, "reason": "missing_current"}
        next1_signal = next1_research.evaluate(
            current_snap=current_snap,
            next1_snap=next1_snap,
            current_secs=current_secs,
            next1_secs=next1_secs,
            reference_price=ref.get("reference_price"),
            source_divergence_bps=ref.get("source_divergence_bps"),
            now_ts=now,
        ) if current_item and next1_item else {"allow": False, "reason": "missing_next1"}

        _append_jsonl(
            log_path,
            {
                "type": "snapshot",
                "ts": now,
                "current_secs": current_secs,
                "next1_secs": next1_secs,
                "reference": ref,
                "current_signal": _current_signal_summary(current_signal),
                "almost_signal": _current_signal_summary(almost_signal),
                "next1_signal": _next1_signal_summary(next1_signal),
                "trades": {key: asdict(value) for key, value in trades.items()},
            },
        )

        setups = (
            ("current_scalp", current_signal, current_exec, current_snap, current_item["slug"] if current_item else None, current_scalp_cfg.target_ticks, current_scalp_cfg.stop_ticks, current_secs),
            ("current_almost_resolved", almost_signal, current_exec, current_snap, current_item["slug"] if current_item else None, almost_cfg.target_ticks, almost_cfg.stop_ticks, current_secs),
            ("next1_scalp", next1_signal, next1_exec, next1_snap, next1_item["slug"] if next1_item else None, next1_cfg.target_ticks, next1_cfg.stop_ticks, next1_secs),
        )

        for setup_key, signal, executable, snap, event_slug, target_ticks, stop_ticks, secs_to_end in setups:
            trade = trades[setup_key]
            if trade.mode == "idle":
                if signal.get("allow"):
                    tick_size = _tick_size_from_snap(snap, signal.get("side") or "UP")
                    trade = _create_trade(
                        setup_key,
                        signal=signal,
                        event_slug=event_slug,
                        tick_size=tick_size,
                        target_ticks=target_ticks,
                        stop_ticks=stop_ticks,
                        now=now,
                    )
                    trades[setup_key] = trade
                    _append_jsonl(log_path, {"type": "entry_posted", "ts": now, "setup_key": setup_key, "signal": signal, "trade": asdict(trade)})
                else:
                    blocked[str(signal.get("reason") or "no_signal")] += 1
                continue

            tick_size = _tick_size_from_snap(snap, trade.side or "UP")
            event_type = None
            if trade.mode == "pending_entry":
                trade, event_type = _manage_entry(
                    trade,
                    signal=signal,
                    executable=executable,
                    tick_size=tick_size,
                    now=now,
                    secs_to_end=secs_to_end,
                    exec_cfg=exec_cfg,
                )
            elif trade.mode == "open":
                trade, event_type = _manage_open(
                    trade,
                    signal=signal,
                    executable=executable,
                    tick_size=tick_size,
                    now=now,
                    secs_to_end=secs_to_end,
                    current_scalp_cfg=current_scalp_cfg,
                    next1_cfg=next1_cfg,
                    almost_cfg=almost_cfg,
                )
            elif trade.mode == "pending_exit":
                trade, event_type = _manage_exit(
                    trade,
                    executable=executable,
                    tick_size=tick_size,
                    now=now,
                    secs_to_end=secs_to_end,
                    exec_cfg=exec_cfg,
                )

            if event_type:
                _append_jsonl(log_path, {"type": event_type, "ts": now, "setup_key": setup_key, "signal": signal, "trade": asdict(trade)})

            if trade.mode == "idle" and trade.exit_reason is not None:
                completed.append(asdict(trade))
                _append_jsonl(log_path, {"type": "exit", "ts": now, "setup_key": setup_key, "trade": completed[-1]})
                trade = _trade_reset(setup_key)

            trades[setup_key] = trade

        time.sleep(max(0.5, float(args.poll_secs)))

    summary = {
        "completed_trades": len(completed),
        "wins": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) > 0),
        "losses": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) < 0),
        "flat": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) == 0),
        "total_pnl_ticks": round(sum(_safe_float(t.get("pnl_ticks")) for t in completed), 4),
        "by_setup": _trade_stats(completed),
        "blocked_reasons": dict(blocked.most_common(20)),
        "log_dir": str(session_dir),
        "log_file": str(log_path),
    }
    print("[SUMMARY]")
    pprint(summary)
    (session_dir / "session_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
