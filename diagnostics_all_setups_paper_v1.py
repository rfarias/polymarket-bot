from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint
from typing import Dict, Optional

from market.continuation_filter_v1 import ContinuationRiskFilterV1
from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.next1_scalp_signal_v1 import Next1ScalpConfigV1, Next1ScalpResearchV1
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _fetch_slot_state, _slot_snapshot
from market.setup1_dryrun_executor_v2 import Setup1DryRunExecutorV2
from market.setup1_policy import ARBITRAGE_SUM_ASKS_MAX, classify_signal
from market.slug_discovery import fetch_event_by_slug


@dataclass
class PaperTrade:
    setup_key: str
    mode: str = "idle"  # idle | pending | open
    side: Optional[str] = None
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    best_bid: Optional[float] = None
    aggressive_entry_price: Optional[float] = None
    passive_entry_price: Optional[float] = None
    aggressive_filled: bool = False
    passive_filled: bool = False
    filled_legs: int = 0
    created_at: float = 0.0
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_ticks: Optional[float] = None
    last_bid: Optional[float] = None


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _default_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"all_setups_paper_{ts}"


def _sanitize_slug(value: Optional[str]) -> str:
    raw = str(value or "unknown").strip().replace("/", "-").replace("\\", "-")
    return raw[:80] if raw else "unknown"


def _round_log_path(session_dir: Path, round_index: int, current_slug: Optional[str], next1_slug: Optional[str]) -> Path:
    return session_dir / f"round_{round_index:02d}__current_{_sanitize_slug(current_slug)}__next1_{_sanitize_slug(next1_slug)}.jsonl"


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


def _secs_from_item(item: Optional[dict]) -> Optional[int]:
    if not item:
        return None
    try:
        return max(0, int(item.get("seconds_to_end")))
    except Exception:
        return None


def _enter_trade(setup_key: str, signal: dict, tick_size: float, now: float, target_ticks: int, stop_ticks: int) -> PaperTrade:
    trade = PaperTrade(setup_key=setup_key)
    trade.mode = "open"
    trade.side = signal.get("side")
    trade.entry_price = _safe_float(signal.get("entry_price"), 0.0)
    trade.aggressive_entry_price = _safe_float(signal.get("aggressive_entry_price"), 0.0)
    trade.passive_entry_price = _safe_float(signal.get("passive_entry_price"), 0.0)
    trade.aggressive_filled = True
    trade.filled_legs = 1
    trade.stop_price = round(max(0.01, trade.entry_price - stop_ticks * tick_size), 6)
    explicit_exit = _safe_float(signal.get("exit_price"), 0.0)
    if explicit_exit > 0:
        trade.target_price = round(min(0.99, explicit_exit), 6)
    else:
        trade.target_price = round(min(0.99, trade.entry_price + target_ticks * tick_size), 6)
    trade.created_at = now
    return trade


def _queue_trade(setup_key: str, signal: dict, now: float) -> PaperTrade:
    trade = PaperTrade(setup_key=setup_key)
    trade.mode = "open"
    trade.side = signal.get("side")
    trade.entry_price = _safe_float(signal.get("aggressive_entry_price"), 0.0)
    trade.aggressive_entry_price = _safe_float(signal.get("aggressive_entry_price"), 0.0)
    trade.passive_entry_price = _safe_float(signal.get("entry_price"), 0.0)
    trade.aggressive_filled = True
    trade.filled_legs = 1
    trade.created_at = now
    return trade


def _pending_expired(trade: PaperTrade, *, secs_to_end: Optional[int], now: float) -> bool:
    if trade.setup_key != "next1_scalp":
        return now - trade.created_at >= 15
    if secs_to_end is not None and secs_to_end < 330:
        return True
    return now - trade.created_at >= 25


def _rebuild_trade_levels(trade: PaperTrade, *, tick_size: float, target_ticks: int, stop_ticks: int) -> PaperTrade:
    trade.stop_price = round(max(0.01, _safe_float(trade.entry_price) - stop_ticks * tick_size), 6)
    trade.target_price = round(min(0.99, _safe_float(trade.entry_price) + target_ticks * tick_size), 6)
    return trade


def _maybe_fill_passive_leg(
    trade: PaperTrade,
    *,
    signal: dict,
    executable: Optional[dict],
    tick_size: float,
    now: float,
    target_ticks: int,
    stop_ticks: int,
    secs_to_end: Optional[int],
) -> PaperTrade:
    if trade.setup_key != "next1_scalp" or trade.mode != "open" or trade.passive_filled:
        return trade
    passive_price = _safe_float(trade.passive_entry_price, 0.0)
    if passive_price <= 0:
        return trade
    if not signal.get("allow") or signal.get("side") != trade.side:
        trade.passive_entry_price = None
        return trade
    ask_now = _ask_for_side(executable, trade.side or "UP")
    if ask_now > 0 and ask_now <= passive_price:
        prior_legs = max(1, int(trade.filled_legs))
        avg_entry = ((_safe_float(trade.entry_price) * prior_legs) + passive_price) / float(prior_legs + 1)
        trade.entry_price = round(avg_entry, 6)
        trade.passive_filled = True
        trade.filled_legs = prior_legs + 1
        trade = _rebuild_trade_levels(trade, tick_size=tick_size, target_ticks=target_ticks, stop_ticks=stop_ticks)
        return trade
    if _pending_expired(trade, secs_to_end=secs_to_end, now=now):
        trade.passive_entry_price = None
    return trade


def _manage_trade(trade: PaperTrade, *, bid_now: float, tick_size: float, now: float, secs_to_end: Optional[int], max_hold_secs: int) -> PaperTrade:
    if trade.mode != "open":
        return trade
    trade.last_bid = bid_now
    trade.best_bid = max(_safe_float(trade.best_bid, 0.0), bid_now)
    if trade.best_bid >= round(_safe_float(trade.entry_price) + tick_size, 6):
        trade.stop_price = max(_safe_float(trade.stop_price), round(trade.best_bid - tick_size, 6))

    if bid_now >= _safe_float(trade.target_price):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "target"
    elif bid_now <= _safe_float(trade.stop_price):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "stop"
    elif secs_to_end is not None and secs_to_end <= 5:
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "deadline"
    elif now - trade.created_at >= max_hold_secs:
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "timeout"

    if trade.mode == "idle" and trade.exit_price is not None and trade.entry_price is not None:
        trade.pnl_ticks = round((trade.exit_price - trade.entry_price) / tick_size, 4)
    return trade


def _close_trade_on_round_roll(trade: PaperTrade, *, tick_size: float) -> PaperTrade:
    if trade.mode != "open":
        return trade
    exit_price = trade.last_bid
    if exit_price is None or exit_price <= 0:
        exit_price = trade.entry_price
    trade.mode = "idle"
    trade.exit_price = exit_price
    trade.exit_reason = "round_roll"
    if trade.entry_price is not None and trade.exit_price is not None:
        trade.pnl_ticks = round((trade.exit_price - trade.entry_price) / tick_size, 4)
    return trade


def _trade_summary(completed: list[dict]) -> dict:
    by_setup: dict = {}
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
    parser = argparse.ArgumentParser(description="Run paper trading for all currently implemented setups")
    parser.add_argument("--seconds", type=int, default=120, help="Run duration")
    parser.add_argument("--poll-secs", type=float, default=2.0, help="Polling interval")
    parser.add_argument("--log-dir", type=str, default=None, help="Optional directory for session logs")
    args = parser.parse_args()

    session_dir = Path(args.log_dir) if args.log_dir else _default_log_path()
    session_dir.mkdir(parents=True, exist_ok=True)
    next1_scalp_cfg = Next1ScalpConfigV1()
    current_scalp_cfg = CurrentScalpConfigV1()
    current_resolved_cfg = CurrentAlmostResolvedConfigV1()
    next1_scalp = Next1ScalpResearchV1(cfg=next1_scalp_cfg)
    current_scalp = CurrentScalpResearchV1(cfg=current_scalp_cfg)
    continuation_filter = ContinuationRiskFilterV1()
    arb_executor = Setup1DryRunExecutorV2()
    session_arb_counters = Counter()

    trades: Dict[str, PaperTrade] = {
        "next1_scalp": PaperTrade(setup_key="next1_scalp"),
        "current_scalp": PaperTrade(setup_key="current_scalp"),
        "current_almost_resolved": PaperTrade(setup_key="current_almost_resolved"),
    }
    last_tick_sizes: Dict[str, float] = {
        "next1_scalp": 0.01,
        "current_scalp": 0.01,
        "current_almost_resolved": 0.01,
    }
    completed_trades: list[dict] = []
    arb_counters = Counter()
    current_open_reference: Dict[str, Optional[float]] = {"slug": None, "price": None, "event_start_time": None}
    session_summary_path = session_dir / "session_summary.json"
    current_round_log: Optional[Path] = None
    current_round_index = 0
    active_round_key: Optional[tuple[str, str]] = None

    print("[ALL_SETUPS_CONFIG]")
    pprint(
        {
            "next1_scalp": next1_scalp_cfg.as_dict(),
            "current_scalp": current_scalp_cfg.as_dict(),
            "current_almost_resolved": current_resolved_cfg.as_dict(),
            "log_dir": str(session_dir),
        }
    )

    started_at = time.time()

    while time.time() - started_at < args.seconds:
        now = time.time()
        slot_bundle = _build_slot_bundle()
        current_item = slot_bundle["queue"].get("current")
        next1_item = slot_bundle["queue"].get("next_1")
        round_key = (
            str(current_item.get("slug") if current_item else ""),
            str(next1_item.get("slug") if next1_item else ""),
        )
        if round_key != active_round_key:
            if active_round_key is not None and current_round_log is not None:
                for setup_key, trade in list(trades.items()):
                    if trade.mode != "open":
                        continue
                    tick_size = max(0.001, _safe_float(last_tick_sizes.get(setup_key), 0.01))
                    trades[setup_key] = _close_trade_on_round_roll(trade, tick_size=tick_size)
                    completed = asdict(trades[setup_key])
                    completed_trades.append(completed)
                    _append_jsonl(current_round_log, {"type": "exit", "ts": now, "setup": setup_key, "trade": completed})
                    trades[setup_key] = PaperTrade(setup_key=setup_key)
            active_round_key = round_key
            current_round_index += 1
            current_round_log = _round_log_path(session_dir, current_round_index, round_key[0], round_key[1])
            arb_executor = Setup1DryRunExecutorV2()
            continuation_filter = ContinuationRiskFilterV1()
            arb_counters = Counter()
            current_open_reference = {"slug": None, "price": None, "event_start_time": None}
            _append_jsonl(
                current_round_log,
                {
                    "type": "round_start",
                    "ts": now,
                    "round_index": current_round_index,
                    "current_slug": round_key[0],
                    "next1_slug": round_key[1],
                },
            )

        slot_state = _fetch_slot_state(slot_bundle)
        current_secs = _secs_from_item(current_item)
        next1_secs = _secs_from_item(next1_item)
        current_snap = _slot_snapshot(slot_state, "current")
        next1_snap = _slot_snapshot(slot_state, "next_1")
        current_exec, current_exec_reason = _compute_executable_metrics(current_snap)
        next1_exec, next1_exec_reason = _compute_executable_metrics(next1_snap)
        reference = fetch_external_btc_reference_v1()

        if current_item and current_item["slug"] != current_open_reference.get("slug"):
            raw_event = fetch_event_by_slug(current_item["slug"])
            market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
            start_time = market.get("eventStartTime") or (raw_event.get("startTime") if raw_event else None)
            opened = fetch_binance_open_price_for_event_start_v1(start_time) if start_time else {"open_price": None}
            current_open_reference = {
                "slug": current_item["slug"],
                "price": opened.get("open_price"),
                "event_start_time": start_time,
            }

        # next1 arb dry-run
        next1_tradable = None
        next1_signal = "idle"
        if next1_exec:
            cont = continuation_filter.update_and_classify(slot_name="next_1", snap=next1_snap, now_ts=now)
            if next1_exec["sum_asks"] <= ARBITRAGE_SUM_ASKS_MAX and not cont.get("block_entry"):
                next1_tradable = next1_exec
            next1_signal = classify_signal(next1_tradable, 2 if next1_tradable else 0, 2)
            arb_logs = arb_executor.process_market_tick(
                slot_name="next_1",
                event_slug=next1_item["slug"] if next1_item else "",
                signal=next1_signal,
                metrics=next1_tradable,
                secs_to_end=next1_secs,
                deadline_trigger=360,
            )
            for line in arb_logs:
                if "[DECISION] next_1: plan_created" in line:
                    arb_counters["plan_created"] += 1
                    session_arb_counters["plan_created"] += 1
                elif "[DECISION] next_1: blocked" in line:
                    arb_counters["blocked"] += 1
                    session_arb_counters["blocked"] += 1
                elif "[PLAN_END] next_1: done" in line:
                    arb_counters["done"] += 1
                    session_arb_counters["done"] += 1
                elif "[PLAN_END] next_1: force_closed" in line:
                    arb_counters["force_closed"] += 1
                    session_arb_counters["force_closed"] += 1
                elif "[PLAN_END] next_1: aborted" in line:
                    arb_counters["aborted"] += 1
                    session_arb_counters["aborted"] += 1
                elif "[FILL]" in line:
                    arb_counters["fills"] += 1
                    session_arb_counters["fills"] += 1
                elif "[EXIT_FILL]" in line:
                    arb_counters["exit_fills"] += 1
                    session_arb_counters["exit_fills"] += 1
        else:
            cont = {"label": "missing_data", "block_entry": True}
            arb_logs = []

        # next1 scalp paper
        next1_scalp_signal = next1_scalp.evaluate(
            current_snap=current_snap,
            next1_snap=next1_snap,
            current_secs=current_secs,
            next1_secs=next1_secs,
            reference_price=reference.get("reference_price"),
            source_divergence_bps=reference.get("source_divergence_bps"),
            now_ts=now,
        )

        # current scalp / almost resolved paper
        current_scalp_signal = current_scalp.evaluate(
            snap=current_snap,
            secs_to_end=current_secs,
            event_start_time=current_open_reference.get("event_start_time"),
            now_ts=now,
            reference_price=reference.get("reference_price"),
            source_divergence_bps=reference.get("source_divergence_bps"),
            opening_reference_price=current_open_reference.get("price"),
        ) if current_item else {"setup": "no_edge", "allow": False}
        current_almost_signal = evaluate_current_almost_resolved_v1(
            snap=current_snap,
            secs_to_end=current_secs,
            reference_signal=current_scalp_signal,
            cfg=current_resolved_cfg,
        ) if current_item else {"setup": "almost_resolved", "allow": False}

        snapshot_row = {
            "type": "snapshot",
            "ts": now,
            "round_index": current_round_index,
            "current_slug": current_item.get("slug") if current_item else None,
            "next1_slug": next1_item.get("slug") if next1_item else None,
            "current_secs": current_secs,
            "next1_secs": next1_secs,
            "reference": reference,
            "next1_arb": {
                "exec_reason": next1_exec_reason,
                "signal": next1_signal,
                "continuation_label": cont.get("label"),
                "continuation_block": cont.get("block_entry"),
                "metrics": next1_exec,
                "logs": arb_logs,
                "snapshot": arb_executor.snapshot(),
            },
            "next1_scalp": next1_scalp_signal,
            "current_scalp": current_scalp_signal,
            "current_almost_resolved": current_almost_signal,
            "trades": {k: asdict(v) for k, v in trades.items()},
        }
        if current_round_log is not None:
            _append_jsonl(current_round_log, snapshot_row)

        setup_inputs = [
            ("next1_scalp", next1_scalp_signal, next1_snap, next1_exec, next1_scalp_cfg.target_ticks, next1_scalp_cfg.stop_ticks, next1_scalp_cfg.max_hold_secs, next1_secs),
            ("current_scalp", current_scalp_signal, current_snap, current_exec, current_scalp_cfg.target_ticks, current_scalp_cfg.stop_ticks, current_scalp_cfg.max_hold_secs, current_secs),
            ("current_almost_resolved", current_almost_signal, current_snap, current_exec, current_resolved_cfg.target_ticks, current_resolved_cfg.stop_ticks, current_resolved_cfg.max_hold_secs, current_secs),
        ]

        for setup_key, signal, snap, executable, target_ticks, stop_ticks, max_hold_secs, secs_to_end in setup_inputs:
            trade = trades[setup_key]
            side_hint = signal.get("side") or trade.side or "UP"
            last_tick_sizes[setup_key] = _tick_size_from_snap(snap, side_hint)
            if trade.mode == "idle" and signal.get("allow"):
                tick_size = last_tick_sizes[setup_key]
                if setup_key == "next1_scalp":
                    trades[setup_key] = _queue_trade(setup_key, signal, now)
                    trades[setup_key] = _rebuild_trade_levels(
                        trades[setup_key],
                        tick_size=tick_size,
                        target_ticks=target_ticks,
                        stop_ticks=stop_ticks,
                    )
                else:
                    trades[setup_key] = _enter_trade(setup_key, signal, tick_size, now, target_ticks, stop_ticks)
                if current_round_log is not None:
                    _append_jsonl(
                        current_round_log,
                        {"type": "enter", "ts": now, "round_index": current_round_index, "setup": setup_key, "signal": signal, "trade": asdict(trades[setup_key])},
                    )

            trade = trades[setup_key]
            if trade.mode == "open" and setup_key == "next1_scalp":
                tick_size = _tick_size_from_snap(snap, trade.side or "UP")
                trade_before = asdict(trade)
                trades[setup_key] = _maybe_fill_passive_leg(
                    trade,
                    signal=signal,
                    executable=executable,
                    tick_size=tick_size,
                    now=now,
                    target_ticks=target_ticks,
                    stop_ticks=stop_ticks,
                    secs_to_end=secs_to_end,
                )
                trade_after = trades[setup_key]
                if not trade_before.get("passive_filled") and trade_after.passive_filled and current_round_log is not None:
                    _append_jsonl(
                        current_round_log,
                        {"type": "add_fill", "ts": now, "round_index": current_round_index, "setup": setup_key, "signal": signal, "trade": asdict(trade_after)},
                    )
                elif trade_before.get("passive_entry_price") and not trade_after.passive_filled and not trade_after.passive_entry_price and current_round_log is not None:
                    if current_round_log is not None:
                        _append_jsonl(
                            current_round_log,
                            {"type": "passive_cancel", "ts": now, "round_index": current_round_index, "setup": setup_key, "signal": signal, "trade": asdict(trade_after)},
                        )

            trade = trades[setup_key]
            if trade.mode == "open":
                tick_size = _tick_size_from_snap(snap, trade.side or "UP")
                bid_now = _bid_for_side(executable, trade.side or "UP")
                trades[setup_key] = _manage_trade(trade, bid_now=bid_now, tick_size=tick_size, now=now, secs_to_end=secs_to_end, max_hold_secs=max_hold_secs)
                if trades[setup_key].mode == "idle" and trades[setup_key].exit_reason is not None:
                    completed = asdict(trades[setup_key])
                    completed_trades.append(completed)
                    if current_round_log is not None:
                        _append_jsonl(current_round_log, {"type": "exit", "ts": now, "round_index": current_round_index, "setup": setup_key, "trade": completed})
                    trades[setup_key] = PaperTrade(setup_key=setup_key)

        time.sleep(max(0.5, float(args.poll_secs)))

    summary = {
        "log_dir": str(session_dir),
        "rounds_seen": current_round_index,
        "completed_trades": len(completed_trades),
        "trade_stats_by_setup": _trade_summary(completed_trades),
        "arb_counters": dict(session_arb_counters),
    }
    print("[SUMMARY]")
    pprint(summary)
    session_summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
