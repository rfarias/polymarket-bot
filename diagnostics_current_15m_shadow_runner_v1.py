from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint
from typing import Optional

from market.current_15m_special_setups_v1 import (
    Current15mSpecialSetupsConfigV1,
    evaluate_counter_reversal_15m_v1,
    evaluate_winner_pullback_15m_v1,
)
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.rest_15m_shadow_public_v1 import build_slot_bundle_15m_v1, fetch_slot_state_15m_v1, slot_snapshot_15m_v1
from market.rest_5m_shadow_public_v4 import _compute_executable_metrics
from market.slug_discovery import fetch_event_by_slug


@dataclass
class ShadowTrade15m:
    setup_key: str
    mode: str = "idle"
    side: Optional[str] = None
    event_slug: Optional[str] = None
    entry_limit_price: Optional[float] = None
    entry_fill_price: Optional[float] = None
    exit_limit_price: Optional[float] = None
    exit_fill_price: Optional[float] = None
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    favorable_polls: int = 0
    reprices: int = 0
    last_reprice_at: float = 0.0
    exit_reason: Optional[str] = None
    pnl_ticks: Optional[float] = None
    best_bid: Optional[float] = None
    hold_to_resolution: bool = False
    max_hold_secs: float = 0.0
    min_visible_entry_size: float = 0.0
    entry_touch_polls: int = 2
    entry_min_age_secs: float = 1.0
    entry_timeout_secs: float = 15.0
    breakeven_on_invalidation: bool = False


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
    return Path("logs") / f"current_15m_shadow_runner_{ts}"


def _tick_size_from_snap(snap: dict, side: str) -> float:
    book_side = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(book_side.get("tick_size"), 0.01))


def _side_book(snap: dict, side: str) -> dict:
    return (snap.get("up") if side == "UP" else snap.get("down")) or {}


def _ask_for_side(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_ask" if side == "UP" else "down_ask"), 0.0)


def _bid_for_side(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _levels_visible_size_at_or_below(levels: list[dict], price_limit: float) -> float:
    total = 0.0
    for lvl in levels or []:
        price = _safe_float((lvl or {}).get("price"), -1.0)
        size = _safe_float((lvl or {}).get("size"), 0.0)
        if 0 < price <= price_limit and size > 0:
            total += size
    return round(total, 6)


def _create_trade(setup_key: str, signal: dict, event_slug: str, now: float) -> ShadowTrade15m:
    return ShadowTrade15m(
        setup_key=setup_key,
        mode="pending_entry",
        side=str(signal.get("side") or ""),
        event_slug=event_slug,
        entry_limit_price=round(_safe_float(signal.get("entry_price"), 0.0), 6),
        target_price=round(_safe_float(signal.get("exit_price"), 0.0), 6),
        stop_price=round(_safe_float(signal.get("stop_price"), 0.0), 6),
        created_at=now,
        updated_at=now,
        last_reprice_at=now,
        hold_to_resolution=bool(signal.get("hold_to_resolution")),
        max_hold_secs=_safe_float(signal.get("max_hold_secs"), 120.0),
        min_visible_entry_size=_safe_float(signal.get("min_visible_entry_size"), 0.0),
        entry_touch_polls=int(signal.get("entry_touch_polls") or 2),
        entry_min_age_secs=_safe_float(signal.get("entry_min_age_secs"), 1.0),
        entry_timeout_secs=_safe_float(signal.get("entry_timeout_secs"), 15.0),
        breakeven_on_invalidation=bool(signal.get("breakeven_on_invalidation")),
    )


def _trade_reset(setup_key: str) -> ShadowTrade15m:
    return ShadowTrade15m(setup_key=setup_key)


def _trade_closed(trade: ShadowTrade15m, *, exit_price: float, tick_size: float, reason: str, now: float) -> ShadowTrade15m:
    trade.mode = "idle"
    trade.exit_fill_price = round(max(0.01, float(exit_price)), 6)
    trade.exit_reason = str(reason)
    trade.updated_at = now
    if trade.entry_fill_price is not None and tick_size > 0:
        trade.pnl_ticks = round((trade.exit_fill_price - float(trade.entry_fill_price)) / tick_size, 4)
    return trade


def _entry_still_valid(trade: ShadowTrade15m, signal: dict) -> bool:
    return bool(signal.get("allow")) and signal.get("side") == trade.side


def _manage_entry(
    trade: ShadowTrade15m,
    *,
    signal: dict,
    executable: Optional[dict],
    snap: dict,
    now: float,
) -> tuple[ShadowTrade15m, Optional[str]]:
    if not _entry_still_valid(trade, signal):
        if now - trade.created_at >= trade.entry_timeout_secs:
            return _trade_reset(trade.setup_key), "entry_signal_invalidated"
        return trade, None

    ask_now = _ask_for_side(executable, trade.side or "UP")
    side_book = _side_book(snap, trade.side or "UP")
    visible_size = _levels_visible_size_at_or_below(side_book.get("top_asks") or [], _safe_float(trade.entry_limit_price))
    if ask_now > 0 and ask_now <= _safe_float(trade.entry_limit_price) and visible_size >= trade.min_visible_entry_size:
        trade.favorable_polls += 1
    else:
        trade.favorable_polls = 0

    if trade.favorable_polls >= trade.entry_touch_polls and now - trade.created_at >= trade.entry_min_age_secs:
        trade.mode = "open"
        trade.entry_fill_price = round(ask_now, 6)
        trade.updated_at = now
        trade.favorable_polls = 0
        return trade, "entry_filled"

    if now - trade.created_at >= max(8.0, trade.entry_timeout_secs):
        return _trade_reset(trade.setup_key), "entry_timeout"
    return trade, None


def _manage_open(
    trade: ShadowTrade15m,
    *,
    signal: dict,
    executable: Optional[dict],
    tick_size: float,
    now: float,
    secs_to_end: Optional[int],
) -> tuple[ShadowTrade15m, Optional[str]]:
    bid_now = _bid_for_side(executable, trade.side or "UP")
    if bid_now <= 0:
        return trade, None

    trade.best_bid = max(_safe_float(trade.best_bid, 0.0), bid_now)

    if trade.setup_key == "winner_pullback_15m":
        if trade.best_bid >= round(_safe_float(trade.entry_fill_price) + tick_size, 6):
            trade.stop_price = max(_safe_float(trade.stop_price), round(trade.best_bid - 2 * tick_size, 6))
        if not trade.hold_to_resolution and bid_now >= _safe_float(trade.target_price):
            trade.mode = "pending_exit"
            trade.exit_limit_price = round(bid_now, 6)
            trade.exit_reason = "target"
            trade.updated_at = now
            return trade, "exit_posted"
        if bid_now <= _safe_float(trade.stop_price):
            trade.mode = "pending_exit"
            trade.exit_limit_price = round(bid_now, 6)
            trade.exit_reason = "stop"
            trade.updated_at = now
            return trade, "exit_posted"
        if trade.hold_to_resolution and secs_to_end is not None and secs_to_end <= 2:
            trade.mode = "pending_exit"
            trade.exit_limit_price = round(bid_now, 6)
            trade.exit_reason = "resolution_hold"
            trade.updated_at = now
            return trade, "exit_posted"
        if (
            _safe_float(trade.entry_fill_price) > 0
            and bid_now >= round(_safe_float(trade.entry_fill_price) + tick_size, 6)
            and (
                signal.get("reason") == "winner_pullback_not_confirmed"
                or abs(_safe_float(signal.get("spot_delta_15s_bps"), 0.0)) > 1.0
            )
        ):
            trade.mode = "pending_exit"
            trade.exit_limit_price = round(bid_now, 6)
            trade.exit_reason = "profit_protect"
            trade.updated_at = now
            return trade, "exit_posted"
    else:
        resume_winner_price = _safe_float(signal.get("resume_winner_price"), 0.985)
        winner_price_now = _safe_float(signal.get("down_price") if trade.side == "UP" else signal.get("up_price"), 0.0)
        if trade.best_bid >= _safe_float(trade.entry_fill_price):
            trade.stop_price = max(_safe_float(trade.stop_price), round(_safe_float(trade.entry_fill_price), 6))
        if bid_now >= _safe_float(trade.target_price):
            trade.mode = "pending_exit"
            trade.exit_limit_price = round(bid_now, 6)
            trade.exit_reason = "target"
            trade.updated_at = now
            return trade, "exit_posted"
        if bid_now <= _safe_float(trade.stop_price):
            trade.mode = "pending_exit"
            trade.exit_limit_price = round(bid_now, 6)
            trade.exit_reason = "stop"
            trade.updated_at = now
            return trade, "exit_posted"
        if winner_price_now >= resume_winner_price and signal.get("reason") == "counter_reversal_not_confirmed":
            trade.mode = "pending_exit"
            trade.exit_limit_price = round(max(_safe_float(trade.entry_fill_price), bid_now), 6) if trade.breakeven_on_invalidation else round(max(0.01, bid_now), 6)
            trade.exit_reason = "breakeven_exit" if trade.breakeven_on_invalidation and bid_now >= _safe_float(trade.entry_fill_price) else "structural_stop"
            trade.updated_at = now
            return trade, "exit_posted"

    if secs_to_end is not None and secs_to_end <= 2:
        trade.mode = "pending_exit"
        trade.exit_limit_price = round(bid_now, 6)
        trade.exit_reason = "deadline"
        trade.updated_at = now
        return trade, "exit_posted"
    if now - trade.created_at >= trade.max_hold_secs:
        trade.mode = "pending_exit"
        trade.exit_limit_price = round(bid_now, 6)
        trade.exit_reason = "timeout"
        trade.updated_at = now
        return trade, "exit_posted"
    return trade, None


def _manage_exit(
    trade: ShadowTrade15m,
    *,
    executable: Optional[dict],
    tick_size: float,
    now: float,
    secs_to_end: Optional[int],
) -> tuple[ShadowTrade15m, Optional[str]]:
    bid_now = _bid_for_side(executable, trade.side or "UP")
    if bid_now > 0 and bid_now >= _safe_float(trade.exit_limit_price):
        trade.favorable_polls += 1
    else:
        trade.favorable_polls = 0

    if bid_now > 0 and trade.favorable_polls >= 1 and now - trade.updated_at >= 0.5:
        return _trade_closed(trade, exit_price=bid_now, tick_size=tick_size, reason=trade.exit_reason or "exit", now=now), "exit_filled"

    if secs_to_end is not None and secs_to_end <= 1 and bid_now > 0:
        return _trade_closed(
            trade,
            exit_price=max(0.01, round(bid_now - tick_size, 6)),
            tick_size=tick_size,
            reason=f"{trade.exit_reason or 'exit'}_force",
            now=now,
        ), "exit_forced"
    return trade, None


def _signal_summary(signal: dict) -> dict:
    return {
        "setup": signal.get("setup"),
        "variant": signal.get("setup_variant"),
        "allow": signal.get("allow"),
        "side": signal.get("side"),
        "reason": signal.get("reason"),
        "entry_price": signal.get("entry_price"),
        "exit_price": signal.get("exit_price"),
        "stop_price": signal.get("stop_price"),
        "hold_to_resolution": signal.get("hold_to_resolution"),
        "distance_usd": signal.get("distance_to_price_to_beat_usd"),
        "distance_bps": signal.get("distance_to_price_to_beat_bps"),
    }


def _trade_stats(completed: list[dict]) -> dict:
    by_setup: dict[str, dict] = {}
    for trade in completed:
        key = str(trade.get("setup_key") or "unknown")
        bucket = by_setup.setdefault(key, {"count": 0, "wins": 0, "losses": 0, "flat": 0, "total_pnl_ticks": 0.0})
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
    parser = argparse.ArgumentParser(description="Current 15m shadow runner with fill/liquidity friction")
    parser.add_argument("--seconds", type=int, default=600, help="Run duration")
    parser.add_argument("--poll-secs", type=float, default=1.0, help="Polling interval")
    parser.add_argument("--log-dir", type=str, default=None, help="Optional session log directory")
    args = parser.parse_args()

    session_dir = Path(args.log_dir) if args.log_dir else _default_log_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "current_15m_shadow_runner.jsonl"

    setup_cfg = Current15mSpecialSetupsConfigV1()
    context_cfg = CurrentScalpConfigV1()
    research = CurrentScalpResearchV1(cfg=context_cfg, history_secs=180)

    print("[CURRENT_15M_SPECIAL_SETUPS_CONFIG]")
    pprint(setup_cfg.as_dict())
    print("[CURRENT_CONTEXT_CONFIG]")
    pprint(context_cfg.as_dict())
    print("[LOG_DIR]")
    print(session_dir)

    completed: list[dict] = []
    blocked = Counter()
    current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}
    trades = {
        "winner_pullback_15m": _trade_reset("winner_pullback_15m"),
        "counter_reversal_15m": _trade_reset("counter_reversal_15m"),
    }

    started_at = time.time()
    while time.time() - started_at < args.seconds:
        now = time.time()
        slot_bundle = build_slot_bundle_15m_v1()
        current_item = slot_bundle["queue"].get("current")
        if not current_item:
            blocked["missing_current_15m"] += 1
            time.sleep(max(0.5, float(args.poll_secs)))
            continue

        slot_state = fetch_slot_state_15m_v1(slot_bundle)
        current_snap = slot_snapshot_15m_v1(slot_state, "current")
        current_exec, _ = _compute_executable_metrics(current_snap)
        current_secs = int(_safe_float(current_item.get("seconds_to_end"), 0.0))

        if current_item["slug"] != current_open_reference.get("slug"):
            raw_event = fetch_event_by_slug(current_item["slug"])
            market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
            start_time = market.get("eventStartTime") or (raw_event.get("startTime") if raw_event else None)
            opened = fetch_binance_open_price_for_event_start_v1(start_time) if start_time else {"open_price": None}
            current_open_reference = {"slug": current_item["slug"], "price": opened.get("open_price"), "event_start_time": start_time}

        ref = fetch_external_btc_reference_v1()
        context_signal = research.evaluate(
            snap=current_snap,
            secs_to_end=current_secs,
            event_start_time=current_open_reference.get("event_start_time"),
            now_ts=now,
            reference_price=ref.get("reference_price"),
            source_divergence_bps=ref.get("source_divergence_bps"),
            opening_reference_price=current_open_reference.get("price"),
        )
        winner_signal = evaluate_winner_pullback_15m_v1(
            snap=current_snap,
            secs_to_end=current_secs,
            reference_signal=context_signal,
            cfg=setup_cfg,
        )
        counter_signal = evaluate_counter_reversal_15m_v1(
            snap=current_snap,
            secs_to_end=current_secs,
            reference_signal=context_signal,
            cfg=setup_cfg,
        )

        _append_jsonl(
            log_path,
            {
                "type": "snapshot",
                "ts": now,
                "current_secs": current_secs,
                "reference": ref,
                "context_signal": {
                    "allow": context_signal.get("allow"),
                    "reason": context_signal.get("reason"),
                    "distance_from_open_bps": context_signal.get("distance_from_open_bps"),
                    "spot_delta_5s_bps": context_signal.get("spot_delta_5s_bps"),
                    "spot_delta_15s_bps": context_signal.get("spot_delta_15s_bps"),
                    "spot_delta_30s_bps": context_signal.get("spot_delta_30s_bps"),
                    "market_delta_5s": context_signal.get("market_delta_5s"),
                    "market_range_60s": context_signal.get("market_range_60s"),
                },
                "winner_signal": _signal_summary(winner_signal),
                "counter_signal": _signal_summary(counter_signal),
                "trades": {key: asdict(value) for key, value in trades.items()},
            },
        )

        any_active = any(t.mode != "idle" for t in trades.values())
        setups = (
            ("winner_pullback_15m", winner_signal),
            ("counter_reversal_15m", counter_signal),
        )
        for setup_key, signal in setups:
            trade = trades[setup_key]
            tick_size = _tick_size_from_snap(current_snap, trade.side or signal.get("side") or "UP")
            event_type = None

            if trade.mode == "idle":
                if any_active:
                    if signal.get("allow"):
                        blocked[f"{setup_key}:blocked_by_other_active_trade"] += 1
                    continue
                if signal.get("allow"):
                    trade = _create_trade(setup_key, signal, current_item["slug"], now)
                    trades[setup_key] = trade
                    any_active = True
                    _append_jsonl(log_path, {"type": "entry_posted", "ts": now, "setup_key": setup_key, "signal": signal, "trade": asdict(trade)})
                else:
                    blocked[f"{setup_key}:{signal.get('reason') or 'no_signal'}"] += 1
                continue

            if trade.mode == "pending_entry":
                trade, event_type = _manage_entry(trade, signal=signal, executable=current_exec, snap=current_snap, now=now)
            elif trade.mode == "open":
                trade, event_type = _manage_open(
                    trade,
                    signal=signal,
                    executable=current_exec,
                    tick_size=tick_size,
                    now=now,
                    secs_to_end=current_secs,
                )
            elif trade.mode == "pending_exit":
                trade, event_type = _manage_exit(
                    trade,
                    executable=current_exec,
                    tick_size=tick_size,
                    now=now,
                    secs_to_end=current_secs,
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
        "blocked_reasons": dict(blocked.most_common(30)),
        "log_dir": str(session_dir),
        "log_file": str(log_path),
    }
    print("[SUMMARY]")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    with (session_dir / "session_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
