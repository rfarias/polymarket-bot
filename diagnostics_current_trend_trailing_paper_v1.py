from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint

from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.current_trend_trailing_signal_v1 import (
    CurrentTrendTrailingConfigV1,
    evaluate_current_trend_trailing_v1,
)
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _fetch_slot_state, _slot_snapshot
from market.slug_discovery import fetch_event_by_slug


@dataclass
class PaperTrade:
    mode: str = "idle"
    slug: str | None = None
    side: str | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    best_bid: float | None = None
    entry_tick_size: float = 0.01
    created_at: float = 0.0
    entry_secs: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_ticks: float | None = None


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


def _build_default_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"current_trend_trailing_paper_{ts}.jsonl"


def _slot_secs_to_end(item: dict | None) -> int | None:
    if not item:
        return None
    try:
        return max(0, int(float(item.get("seconds_to_end"))))
    except Exception:
        return None


def _tick_size_from_snap(snap: dict, side: str) -> float:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(side_book.get("tick_size"), 0.01))


def _bid_for_side(executable: dict | None, side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _paper_enter(signal: dict, *, tick_size: float, now: float, slug: str, secs_to_end: int | None) -> PaperTrade:
    entry = _safe_float(signal.get("entry_price"), 0.0)
    return PaperTrade(
        mode="open",
        slug=slug,
        side=str(signal.get("side") or ""),
        entry_price=entry,
        stop_price=_safe_float(signal.get("stop_price"), max(0.01, entry - _safe_float(signal.get("initial_stop_ticks"), 4) * tick_size)),
        target_price=_safe_float(signal.get("target_price"), 0.98),
        best_bid=_safe_float(signal.get("exit_price"), 0.0),
        entry_tick_size=tick_size,
        created_at=now,
        entry_secs=secs_to_end,
    )


def _paper_manage(
    trade: PaperTrade,
    *,
    bid_now: float,
    tick_size: float,
    now: float,
    secs_to_end: int | None,
    signal: dict,
    cfg: CurrentTrendTrailingConfigV1,
) -> PaperTrade:
    if trade.mode != "open":
        return trade
    if bid_now <= 0:
        return trade

    entry = _safe_float(trade.entry_price)
    trade.best_bid = max(_safe_float(trade.best_bid), bid_now)
    if _safe_float(trade.best_bid) >= entry + cfg.arm_after_ticks * tick_size:
        trailing_stop = round(max(0.01, _safe_float(trade.best_bid) - cfg.trailing_stop_ticks * tick_size), 6)
        trade.stop_price = max(_safe_float(trade.stop_price), trailing_stop)

    if bid_now >= _safe_float(trade.target_price):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "target"
    elif bid_now <= _safe_float(trade.stop_price):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "trailing_stop"
    elif secs_to_end is not None and secs_to_end <= 1:
        distance = _safe_float(signal.get("distance_from_open_bps"), 0.0)
        final_win = (trade.side == "UP" and distance > 0) or (trade.side == "DOWN" and distance < 0)
        trade.mode = "idle"
        trade.exit_price = 1.0 if final_win else 0.0
        trade.exit_reason = "resolution"

    if trade.mode == "idle" and trade.exit_price is not None:
        trade.pnl_ticks = round((_safe_float(trade.exit_price) - entry) / tick_size, 4)
    return trade


def _force_close_trade(trade: PaperTrade, *, bid_now: float = 0.0) -> PaperTrade:
    if trade.mode != "open":
        return trade
    trade.mode = "idle"
    trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
    trade.exit_reason = "session_end"
    trade.pnl_ticks = round((_safe_float(trade.exit_price) - _safe_float(trade.entry_price)) / _safe_float(trade.entry_tick_size, 0.01), 4)
    return trade


def _trade_stats(completed: list[dict]) -> dict:
    total = round(sum(_safe_float(t.get("pnl_ticks")) for t in completed), 4)
    count = len(completed)
    wins = sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) > 0)
    losses = sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) < 0)
    return {
        "completed_trades": count,
        "wins": wins,
        "losses": losses,
        "flat": count - wins - losses,
        "win_rate": round(100.0 * wins / count, 2) if count else 0.0,
        "total_pnl_ticks": total,
        "avg_pnl_ticks": round(total / count, 4) if count else 0.0,
        "max_win_ticks": max((_safe_float(t.get("pnl_ticks")) for t in completed), default=0.0),
        "max_loss_ticks": min((_safe_float(t.get("pnl_ticks")) for t in completed), default=0.0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trade current 5m trend following with trailing stop")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--poll-secs", type=float, default=1.0)
    parser.add_argument("--log-file", type=str, default=None)
    args = parser.parse_args()

    scalp_cfg = CurrentScalpConfigV1()
    trend_cfg = CurrentTrendTrailingConfigV1()
    scalp = CurrentScalpResearchV1(cfg=scalp_cfg)
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    trade = PaperTrade()
    completed: list[dict] = []
    blocked_reasons = Counter()
    allowed_sides = Counter()
    exit_reasons = Counter()
    current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}

    print("[CURRENT_TREND_TRAILING_CONFIG]")
    pprint(trend_cfg.as_dict())
    print("[LOG_FILE]")
    print(log_path)

    started_at = time.time()
    while time.time() - started_at < args.seconds:
        now = time.time()
        slot_bundle = _build_slot_bundle()
        current_item = slot_bundle["queue"].get("current")
        if not current_item:
            print("[CURRENT_TREND_TRAILING] current slot unavailable")
            time.sleep(max(0.5, float(args.poll_secs)))
            continue

        current_slug = str(current_item.get("slug") or "")
        current_secs = _slot_secs_to_end(current_item)
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
        scalp_signal = scalp.evaluate(
            snap=snap,
            secs_to_end=current_secs,
            event_start_time=current_open_reference.get("event_start_time"),
            now_ts=now,
            reference_price=ref.get("reference_price"),
            source_divergence_bps=ref.get("source_divergence_bps"),
            opening_reference_price=current_open_reference.get("price"),
        )
        signal = evaluate_current_trend_trailing_v1(scalp_signal, trend_cfg)
        signal["event_slug"] = current_slug

        if signal.get("allow"):
            allowed_sides[str(signal.get("side") or "NONE")] += 1
        else:
            blocked_reasons[str(signal.get("reason") or "unknown")] += 1

        if trade.mode == "open" and trade.slug != current_slug:
            bid_now = _bid_for_side(executable, trade.side or "")
            trade = _force_close_trade(trade, bid_now=bid_now)
            completed.append(asdict(trade))
            exit_reasons[str(trade.exit_reason or "unknown")] += 1
            _append_jsonl(log_path, {"type": "exit", "ts": now, "trade": completed[-1]})
            trade = PaperTrade()

        if trade.mode == "idle" and signal.get("allow"):
            side = str(signal.get("side") or "")
            tick_size = _tick_size_from_snap(snap, side)
            trade = _paper_enter(signal, tick_size=tick_size, now=now, slug=current_slug, secs_to_end=current_secs)
            _append_jsonl(log_path, {"type": "enter", "ts": now, "signal": signal, "trade": asdict(trade)})
        elif trade.mode == "open":
            side = trade.side or ""
            tick_size = _tick_size_from_snap(snap, side)
            bid_now = _bid_for_side(executable, side)
            trade = _paper_manage(
                trade,
                bid_now=bid_now,
                tick_size=tick_size,
                now=now,
                secs_to_end=current_secs,
                signal=signal,
                cfg=trend_cfg,
            )
            if trade.mode == "idle":
                completed.append(asdict(trade))
                exit_reasons[str(trade.exit_reason or "unknown")] += 1
                _append_jsonl(log_path, {"type": "exit", "ts": now, "trade": completed[-1]})
                trade = PaperTrade()

        _append_jsonl(
            log_path,
            {
                "type": "snapshot",
                "ts": now,
                "current_slug": current_slug,
                "current_secs": current_secs,
                "executable_reason": executable_reason,
                "reference": ref,
                "current_scalp_context": scalp_signal,
                "signal": signal,
                "trade": asdict(trade),
            },
        )

        print(
            f"[CURRENT_TREND_TRAILING] secs={current_secs} allow={signal.get('allow')} "
            f"side={signal.get('side')} reason={signal.get('reason')} mode={trade.mode}"
        )
        time.sleep(max(0.5, float(args.poll_secs)))

    if trade.mode == "open":
        trade = _force_close_trade(trade)
        completed.append(asdict(trade))
        exit_reasons[str(trade.exit_reason or "unknown")] += 1
        _append_jsonl(log_path, {"type": "exit", "ts": time.time(), "trade": completed[-1]})

    summary = {
        "stats": _trade_stats(completed),
        "allowed_sides": dict(allowed_sides),
        "exit_reasons": dict(exit_reasons),
        "top_blocked_reasons": blocked_reasons.most_common(12),
        "log_file": str(log_path),
        "config": trend_cfg.as_dict(),
    }
    _append_jsonl(log_path, {"type": "summary", "ts": time.time(), "summary": summary})
    print("[CURRENT_TREND_TRAILING_SUMMARY]")
    pprint(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
