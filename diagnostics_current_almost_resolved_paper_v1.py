from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint

from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.rest_5m_shadow_public_v5 import (
    _build_slot_bundle,
    _compute_executable_metrics,
    _fetch_slot_state,
    _slot_snapshot,
)
from market.slug_discovery import fetch_event_by_slug


@dataclass
class PaperTrade:
    mode: str = "idle"  # idle | open
    side: str | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    best_bid: float | None = None
    created_at: float = 0.0
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_ticks: float | None = None
    hold_to_resolution: bool = False


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
    return Path("logs") / f"current_almost_resolved_paper_{ts}.jsonl"


def _tick_size_from_snap(snap: dict, side: str) -> float:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(side_book.get("tick_size"), 0.01))


def _bid_for_side(executable: dict | None, side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _paper_enter(signal: dict, tick_size: float, now: float, cfg: CurrentAlmostResolvedConfigV1) -> PaperTrade:
    trade = PaperTrade()
    trade.mode = "open"
    trade.side = signal.get("side")
    trade.entry_price = _safe_float(signal.get("entry_price"), 0.0)
    trade.target_price = round(min(0.99, _safe_float(signal.get("exit_price"), 0.99)), 6)
    trade.stop_price = round(max(0.01, trade.entry_price - cfg.stop_ticks * tick_size), 6)
    trade.created_at = now
    return trade


def _paper_manage(
    trade: PaperTrade,
    *,
    bid_now: float,
    tick_size: float,
    now: float,
    secs_to_end: int | None,
    signal: dict,
    cfg: CurrentAlmostResolvedConfigV1,
) -> PaperTrade:
    if trade.mode != "open":
        return trade
    trade.best_bid = max(_safe_float(trade.best_bid, 0.0), bid_now)
    pnl_ticks_now = (bid_now - _safe_float(trade.entry_price, 0.0)) / tick_size if tick_size > 0 else 0.0
    side = trade.side or "UP"
    buffer_bps = _safe_float(
        signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"),
        0.0,
    )
    open_distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    market_range_30s = _safe_float(signal.get("market_range_30s"), 0.0)
    edge_vs_counter = _safe_float(signal.get("up_edge_vs_counter" if side == "UP" else "down_edge_vs_counter"), 0.0)
    adverse_spot_bps = _safe_float(signal.get("up_adverse_spot_bps" if side == "UP" else "down_adverse_spot_bps"), 0.0)

    if (
        secs_to_end is not None
        and secs_to_end <= cfg.paper_hold_to_resolution_secs
        and bid_now >= cfg.paper_hold_to_resolution_min_price
        and buffer_bps >= cfg.paper_hold_to_resolution_min_buffer_bps
        and open_distance_bps >= cfg.paper_hold_to_resolution_min_open_distance_bps
        and market_range_30s <= cfg.paper_profit_take_on_market_range_30s
    ):
        trade.hold_to_resolution = True

    if bid_now >= _safe_float(trade.target_price):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "target"
    elif bid_now <= _safe_float(trade.stop_price):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "stop"
    elif (
        pnl_ticks_now >= cfg.paper_profit_take_min_ticks
        and (
            (secs_to_end is not None and secs_to_end <= cfg.paper_profit_take_late_secs)
            or buffer_bps <= cfg.paper_profit_take_on_reversal_buffer_bps
            or market_range_30s >= cfg.paper_profit_take_on_market_range_30s
            or adverse_spot_bps >= open_distance_bps * cfg.max_reversal_share_of_open_distance
        )
    ):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "profit_protect"
    elif (
        pnl_ticks_now > 0
        and not trade.hold_to_resolution
        and secs_to_end is not None
        and secs_to_end <= cfg.paper_hold_to_resolution_secs
    ):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "late_profit_take"
    elif (
        buffer_bps <= cfg.paper_structural_stop_buffer_bps
        or market_range_30s >= cfg.paper_structural_stop_market_range_30s
        or edge_vs_counter <= cfg.paper_structural_stop_edge_vs_counter
        or (signal.get("side") not in (None, side) and signal.get("allow"))
    ):
        trade.mode = "idle"
        trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
        trade.exit_reason = "structural_stop"
    elif secs_to_end is not None and secs_to_end <= cfg.min_secs_to_end:
        trade.mode = "idle"
        trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
        trade.exit_reason = "deadline"
    elif not trade.hold_to_resolution and now - trade.created_at >= cfg.max_hold_secs:
        trade.mode = "idle"
        trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
        trade.exit_reason = "timeout"

    if trade.mode == "idle" and trade.exit_price is not None and trade.entry_price is not None:
        trade.pnl_ticks = round((trade.exit_price - trade.entry_price) / tick_size, 4)
    return trade


def _trade_stats(completed: list[dict]) -> dict:
    total_pnl_ticks = round(sum(_safe_float(t.get("pnl_ticks")) for t in completed), 4)
    return {
        "completed_trades": len(completed),
        "wins": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) > 0),
        "losses": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) < 0),
        "flat": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) == 0),
        "total_pnl_ticks": total_pnl_ticks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trade the current almost-resolved setup in isolation")
    parser.add_argument("--seconds", type=int, default=300, help="Run duration")
    parser.add_argument("--poll-secs", type=float, default=2.0, help="Polling interval")
    parser.add_argument("--log-file", type=str, default=None, help="Optional JSONL log path")
    args = parser.parse_args()

    signal_cfg = CurrentAlmostResolvedConfigV1()
    scalp_cfg = CurrentScalpConfigV1()
    current_scalp = CurrentScalpResearchV1(cfg=scalp_cfg)
    trade = PaperTrade()
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    completed: list[dict] = []
    blocked_reasons = Counter()

    print("[CURRENT_ALMOST_RESOLVED_CONFIG]")
    pprint(signal_cfg.as_dict())
    print("[CURRENT_SCALP_CONTEXT_CONFIG]")
    pprint(scalp_cfg.as_dict())
    print("[LOG_FILE]")
    print(log_path)

    started_at = time.time()
    current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}

    while time.time() - started_at < args.seconds:
        now = time.time()
        slot_bundle = _build_slot_bundle()
        current_item = slot_bundle["queue"].get("current")
        if not current_item:
            print("[CURRENT_ALMOST_RESOLVED] current slot unavailable")
            time.sleep(max(0.5, float(args.poll_secs)))
            continue

        if current_item.get("slug") != current_open_reference.get("slug"):
            raw_event = fetch_event_by_slug(str(current_item.get("slug") or ""))
            market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
            event_start_time = market.get("eventStartTime") or raw_event.get("startTime") if raw_event else None
            open_ref = fetch_binance_open_price_for_event_start_v1(event_start_time)
            current_open_reference = {
                "slug": current_item.get("slug"),
                "price": open_ref.get("open_price"),
                "event_start_time": event_start_time,
            }

        slot_state = _fetch_slot_state(slot_bundle)
        current_snap = _slot_snapshot(slot_state, "current")
        current_exec, current_exec_reason = _compute_executable_metrics(current_snap)
        current_secs = _slot_secs_to_end(current_item)
        reference = fetch_external_btc_reference_v1()
        current_scalp_signal = current_scalp.evaluate(
            snap=current_snap,
            secs_to_end=current_secs,
            event_start_time=current_open_reference.get("event_start_time"),
            now_ts=now,
            reference_price=reference.get("reference_price"),
            source_divergence_bps=reference.get("source_divergence_bps"),
            opening_reference_price=current_open_reference.get("price"),
        )
        signal = evaluate_current_almost_resolved_v1(
            snap=current_snap,
            secs_to_end=current_secs,
            reference_signal=current_scalp_signal,
            cfg=signal_cfg,
        )

        if not signal.get("allow"):
            blocked_reasons[str(signal.get("reason") or "unknown")] += 1

        print("\n===== CURRENT ALMOST RESOLVED PAPER V1 =====")
        print(
            f"current_secs={current_secs} exec_reason={current_exec_reason} "
            f"allow={signal.get('allow')} side={signal.get('side')} trade_mode={trade.mode}"
        )
        print("[SIGNAL]")
        pprint(signal)

        _append_jsonl(
            log_path,
            {
                "type": "snapshot",
                "ts": now,
                "current_slug": current_item.get("slug"),
                "current_secs": current_secs,
                "reference": reference,
                "current_scalp_context": current_scalp_signal,
                "signal": signal,
                "trade": asdict(trade),
            },
        )

        if trade.mode == "idle" and signal.get("allow"):
            tick_size = _tick_size_from_snap(current_snap, signal.get("side") or "UP")
            trade = _paper_enter(signal, tick_size, now, signal_cfg)
            _append_jsonl(log_path, {"type": "enter", "ts": now, "signal": signal, "trade": asdict(trade)})
            print("[PAPER_ENTER]")
            pprint(asdict(trade))

        if trade.mode == "open":
            tick_size = _tick_size_from_snap(current_snap, trade.side or "UP")
            bid_now = _bid_for_side(current_exec, trade.side or "UP")
            trade = _paper_manage(
                trade,
                bid_now=bid_now,
                tick_size=tick_size,
                now=now,
                secs_to_end=current_secs,
                signal=signal,
                cfg=signal_cfg,
            )
            print("[PAPER_MANAGE]")
            pprint(asdict(trade))
            if trade.mode == "idle" and trade.exit_reason is not None:
                completed.append(asdict(trade))
                _append_jsonl(log_path, {"type": "exit", "ts": now, "trade": completed[-1]})
                print("[PAPER_EXIT]")
                pprint(completed[-1])
                trade = PaperTrade()

        time.sleep(max(0.5, float(args.poll_secs)))

    print("\n[SUMMARY]")
    pprint(
        {
            "stats": _trade_stats(completed),
            "blocked_reasons_top10": blocked_reasons.most_common(10),
            "log_file": str(log_path),
            "trades": completed,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
