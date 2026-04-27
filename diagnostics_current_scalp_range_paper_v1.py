from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint

from market.current_scalp_signal_v1 import (
    CurrentScalpRangeConfigV1,
    CurrentScalpRangePermissiveConfigV1,
    CurrentScalpRangeResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _fetch_slot_state, _slot_snapshot
from market.slug_discovery import fetch_event_by_slug


@dataclass
class PaperTrade:
    mode: str = "idle"
    side: str | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    best_bid: float | None = None
    entry_tick_size: float = 0.01
    created_at: float = 0.0
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_ticks: float | None = None


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_default_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"current_scalp_range_paper_{ts}.jsonl"


def _tick_size_from_snap(snap: dict, side: str) -> float:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(side_book.get("tick_size"), 0.01))


def _bid_for_side(executable: dict | None, side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _paper_enter(signal: dict, tick_size: float, now: float, cfg: CurrentScalpRangeConfigV1) -> PaperTrade:
    trade = PaperTrade()
    trade.mode = "open"
    trade.side = str(signal.get("side") or "UP")
    trade.entry_price = _safe_float(signal.get("entry_price"), 0.0)
    trade.target_price = round(min(0.99, trade.entry_price + cfg.target_ticks * tick_size), 6)
    trade.stop_price = round(max(0.01, trade.entry_price - cfg.stop_ticks * tick_size), 6)
    trade.entry_tick_size = tick_size
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
    cfg: CurrentScalpRangeConfigV1,
) -> PaperTrade:
    if trade.mode != "open":
        return trade

    trade.best_bid = max(_safe_float(trade.best_bid, 0.0), bid_now)
    if trade.best_bid >= round(_safe_float(trade.entry_price) + tick_size, 6):
        trade.stop_price = max(_safe_float(trade.stop_price), _safe_float(trade.entry_price))
    market_delta_5s = _safe_float(signal.get("market_delta_5s"), 0.0)
    spot_delta_5s_bps = _safe_float(signal.get("spot_delta_5s_bps"), 0.0)

    if bid_now >= _safe_float(trade.target_price):
        trade.mode = "idle"
        trade.exit_price = _safe_float(trade.target_price)
        trade.exit_reason = "target"
    elif bid_now <= _safe_float(trade.stop_price):
        trade.mode = "idle"
        trade.exit_price = _safe_float(trade.stop_price)
        trade.exit_reason = "stop"
    elif secs_to_end is not None and secs_to_end <= cfg.min_secs_to_end:
        trade.mode = "idle"
        trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
        trade.exit_reason = "deadline"
    elif now - trade.created_at >= cfg.max_hold_secs:
        trade.mode = "idle"
        trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
        trade.exit_reason = "timeout"
    elif trade.side == "UP" and (market_delta_5s < 0 or spot_delta_5s_bps < 0):
        trade.mode = "idle"
        trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
        trade.exit_reason = "reversal_failed"
    elif trade.side == "DOWN" and (market_delta_5s > 0 or spot_delta_5s_bps > 0):
        trade.mode = "idle"
        trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
        trade.exit_reason = "reversal_failed"
    elif signal.get("allow") and signal.get("side") and signal.get("side") != trade.side:
        trade.mode = "idle"
        trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
        trade.exit_reason = "opposite_extreme"

    if trade.mode == "idle" and trade.exit_price is not None and trade.entry_price is not None:
        trade.pnl_ticks = round((trade.exit_price - trade.entry_price) / _safe_float(trade.entry_tick_size, tick_size or 0.01), 4)
    return trade


def _trade_stats(completed: list[dict]) -> dict:
    total_pnl_ticks = round(sum(_safe_float(t.get("pnl_ticks")) for t in completed), 4)
    count = len(completed)
    avg_pnl = round(total_pnl_ticks / count, 4) if count else 0.0
    return {
        "completed_trades": count,
        "wins": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) > 0),
        "losses": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) < 0),
        "flat": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) == 0),
        "total_pnl_ticks": total_pnl_ticks,
        "avg_pnl_ticks": avg_pnl,
    }


def _force_close_trade(trade: PaperTrade) -> PaperTrade:
    if trade.mode != "open":
        return trade
    trade.mode = "idle"
    trade.exit_price = trade.entry_price
    trade.exit_reason = "session_end"
    trade.pnl_ticks = 0.0
    return trade


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trade current scalp range reversion in isolation")
    parser.add_argument("--seconds", type=int, default=300, help="Run duration")
    parser.add_argument("--poll-secs", type=float, default=2.0, help="Polling interval")
    parser.add_argument("--log-file", type=str, default=None, help="Optional JSONL log path")
    parser.add_argument("--variant", type=str, default="conservative", choices=("conservative", "permissive"), help="Range subvariant")
    args = parser.parse_args()

    cfg = CurrentScalpRangePermissiveConfigV1() if args.variant == "permissive" else CurrentScalpRangeConfigV1()
    research = CurrentScalpRangeResearchV1(cfg=cfg)
    trade = PaperTrade()
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    completed: list[dict] = []
    blocked_reasons = Counter()
    allowed_sides = Counter()
    exit_reasons = Counter()

    print("[CURRENT_SCALP_RANGE_CONFIG]")
    pprint(cfg.as_dict())
    print("[VARIANT]")
    print(args.variant)
    print("[LOG_FILE]")
    print(log_path)

    started_at = time.time()
    current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}

    while time.time() - started_at < args.seconds:
        now = time.time()
        slot_bundle = _build_slot_bundle()
        current_item = slot_bundle["queue"].get("current")
        if not current_item:
            print("[CURRENT_SCALP_RANGE] current slot unavailable")
            time.sleep(max(0.5, float(args.poll_secs)))
            continue

        current_slug = str(current_item.get("slug") or "")
        if current_open_reference["slug"] != current_slug:
            raw_event = fetch_event_by_slug(current_slug) if current_slug else None
            market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
            event_start_time = market.get("eventStartTime") or (raw_event or {}).get("startTime")
            open_ref = fetch_binance_open_price_for_event_start_v1(event_start_time) if event_start_time else {"open_price": None}
            current_open_reference = {
                "slug": current_slug,
                "price": open_ref.get("open_price"),
                "event_start_time": event_start_time,
            }

        slot_state = _fetch_slot_state(slot_bundle)
        snap = _slot_snapshot(slot_state, "current")
        executable, executable_reason = _compute_executable_metrics(snap)
        ref = fetch_external_btc_reference_v1()
        secs_to_end = current_item.get("seconds_to_end")
        try:
            secs_to_end = max(0, int(secs_to_end)) if secs_to_end is not None else None
        except Exception:
            secs_to_end = None

        signal = research.evaluate(
            snap=snap,
            secs_to_end=secs_to_end,
            event_start_time=current_open_reference.get("event_start_time"),
            now_ts=now,
            reference_price=ref.get("reference_price"),
            source_divergence_bps=ref.get("source_divergence_bps"),
            opening_reference_price=current_open_reference.get("price"),
        )

        snapshot = {
            "type": "snapshot",
            "ts": now,
            "variant": args.variant,
            "slug": current_slug,
            "secs_to_end": secs_to_end,
            "executable_reason": executable_reason,
            "signal": signal,
            "reference": ref,
            "trade": asdict(trade),
        }
        _append_jsonl(log_path, snapshot)

        if signal.get("allow"):
            allowed_sides[str(signal.get("side") or "NONE")] += 1
        else:
            blocked_reasons[str(signal.get("reason") or "unknown")] += 1

        if trade.mode == "idle" and signal.get("allow"):
            side = str(signal.get("side") or "UP")
            tick_size = _tick_size_from_snap(snap, side)
            trade = _paper_enter(signal, tick_size, now, cfg)
            _append_jsonl(log_path, {"type": "enter", "ts": now, "variant": args.variant, "signal": signal, "trade": asdict(trade)})
        elif trade.mode == "open":
            side = trade.side or "UP"
            tick_size = _tick_size_from_snap(snap, side)
            bid_now = _bid_for_side(executable, side)
            trade = _paper_manage(
                trade,
                bid_now=bid_now,
                tick_size=tick_size,
                now=now,
                secs_to_end=secs_to_end,
                signal=signal,
                cfg=cfg,
            )
            if trade.mode == "idle":
                completed.append(asdict(trade))
                exit_reasons[str(trade.exit_reason or "unknown")] += 1
                _append_jsonl(log_path, {"type": "exit", "ts": now, "variant": args.variant, "trade": completed[-1]})
                trade = PaperTrade()

        time.sleep(max(0.5, float(args.poll_secs)))

    if trade.mode == "open":
        trade = _force_close_trade(trade)
        completed.append(asdict(trade))
        exit_reasons[str(trade.exit_reason or "unknown")] += 1
        _append_jsonl(log_path, {"type": "exit", "ts": time.time(), "variant": args.variant, "trade": completed[-1]})
        trade = PaperTrade()

    summary = {
        "variant": args.variant,
        "stats": _trade_stats(completed),
        "allowed_sides": dict(allowed_sides),
        "exit_reasons": dict(exit_reasons),
        "top_blocked_reasons": blocked_reasons.most_common(10),
        "log_file": str(log_path),
    }
    _append_jsonl(log_path, {"type": "summary", "ts": time.time(), "summary": summary})
    print("[CURRENT_SCALP_RANGE_SUMMARY]")
    pprint(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
