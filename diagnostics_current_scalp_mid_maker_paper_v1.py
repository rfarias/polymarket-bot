from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint

from market.current_scalp_signal_v1 import (
    CurrentScalpMidMakerConfigV1,
    CurrentScalpMidMakerResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.rest_5m_shadow_public_v5 import _compute_executable_metrics, _build_slot_bundle, _fetch_slot_state, _slot_snapshot
from market.slug_discovery import fetch_event_by_slug


@dataclass
class MakerPaperTrade:
    mode: str = "idle"  # idle | pending_entry | open
    side: str | None = None
    entry_price: float | None = None
    target_price: float | None = None
    stop_price: float | None = None
    tick_size: float = 0.01
    created_at: float = 0.0
    filled_at: float | None = None
    queue_ahead: float = 0.0
    touch_count: int = 0
    best_bid_seen: float | None = None
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
    return Path("logs") / f"current_scalp_mid_maker_paper_{ts}.jsonl"


def _tick_size_from_snap(snap: dict, side: str) -> float:
    side_book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(side_book.get("tick_size"), 0.01))


def _best_bid_for_side(executable: dict | None, side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _best_ask_for_side(executable: dict | None, side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_ask" if side == "UP" else "down_ask"), 0.0)


def _levels_for_side(snap: dict, side: str) -> tuple[list[dict], list[dict]]:
    book = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return list(book.get("top_bids") or []), list(book.get("top_asks") or [])


def _size_at_price(levels: list[dict], price: float) -> float:
    for level in levels:
        if abs(_safe_float(level.get("price"), -1.0) - float(price)) < 0.0005:
            return max(0.0, _safe_float(level.get("size"), 0.0))
    return 0.0


def _paper_place_entry(signal: dict, tick_size: float, now: float, snap: dict, cfg: CurrentScalpMidMakerConfigV1) -> MakerPaperTrade:
    trade = MakerPaperTrade()
    trade.mode = "pending_entry"
    trade.side = str(signal.get("side") or "UP")
    trade.entry_price = _safe_float(signal.get("entry_price"), 0.0)
    trade.target_price = round(min(0.99, trade.entry_price + cfg.maker_target_ticks * tick_size), 6)
    trade.stop_price = round(max(0.01, trade.entry_price - cfg.maker_stop_ticks * tick_size), 6)
    trade.tick_size = tick_size
    trade.created_at = now
    bid_levels, _ = _levels_for_side(snap, trade.side)
    trade.queue_ahead = _size_at_price(bid_levels, trade.entry_price)
    return trade


def _maybe_fill_pending_entry(trade: MakerPaperTrade, *, snap: dict, executable: dict | None, signal: dict, now: float) -> MakerPaperTrade:
    if trade.mode != "pending_entry" or not trade.side or trade.entry_price is None:
        return trade
    best_bid = _best_bid_for_side(executable, trade.side)
    best_ask = _best_ask_for_side(executable, trade.side)
    bid_levels, ask_levels = _levels_for_side(snap, trade.side)
    best_bid_size = _safe_float((bid_levels[0] or {}).get("size"), 0.0) if bid_levels else 0.0
    best_ask_size = _safe_float((ask_levels[0] or {}).get("size"), 0.0) if ask_levels else 0.0
    spread = round(max(0.0, best_ask - best_bid), 6) if best_bid > 0 and best_ask > 0 else 0.0

    if best_ask > 0 and best_ask <= trade.entry_price:
        trade.mode = "open"
        trade.filled_at = now
        trade.best_bid_seen = best_bid
        return trade

    if abs(best_bid - trade.entry_price) < 0.0005:
        trade.touch_count += 1
        queue_refresh = _size_at_price(bid_levels, trade.entry_price)
        trade.queue_ahead = max(trade.queue_ahead, queue_refresh)
        consume_proxy = 0.0
        if best_ask_size > 0:
            consume_proxy += best_ask_size * 0.18
        if str(signal.get("reason") or "") == "no_mid_book_reentry":
            consume_proxy += 0.25
        if spread <= trade.tick_size:
            consume_proxy += 0.15
        if trade.touch_count >= 3:
            consume_proxy += 0.35
        if best_bid_size > 0:
            consume_proxy += min(0.5, 1.0 / max(1.0, best_bid_size))
        trade.queue_ahead = max(0.0, trade.queue_ahead - consume_proxy)
        if trade.queue_ahead <= 0.0:
            trade.mode = "open"
            trade.filled_at = now
            trade.best_bid_seen = best_bid
            return trade

    if best_bid > trade.entry_price:
        trade.mode = "idle"
        trade.exit_reason = "missed_reprice"
        return trade

    return trade


def _manage_open_trade(
    trade: MakerPaperTrade,
    *,
    executable: dict | None,
    signal: dict,
    now: float,
    secs_to_end: int | None,
    cfg: CurrentScalpMidMakerConfigV1,
) -> MakerPaperTrade:
    if trade.mode != "open" or not trade.side:
        return trade
    best_bid = _best_bid_for_side(executable, trade.side)
    best_ask = _best_ask_for_side(executable, trade.side)
    trade.best_bid_seen = max(_safe_float(trade.best_bid_seen, 0.0), best_bid)

    if best_ask >= _safe_float(trade.target_price):
        trade.mode = "idle"
        trade.exit_price = _safe_float(trade.target_price)
        trade.exit_reason = "aggressive_target"
    elif best_bid <= _safe_float(trade.stop_price):
        trade.mode = "idle"
        trade.exit_price = _safe_float(trade.stop_price)
        trade.exit_reason = "stop"
    elif secs_to_end is not None and secs_to_end <= cfg.min_secs_to_end:
        trade.mode = "idle"
        trade.exit_price = best_bid if best_bid > 0 else trade.entry_price
        trade.exit_reason = "deadline"
    elif now - (trade.filled_at or trade.created_at) >= cfg.maker_max_hold_secs:
        trade.mode = "idle"
        trade.exit_price = best_bid if best_bid > 0 else trade.entry_price
        trade.exit_reason = "timeout"
    elif signal.get("regime") != "mid_book_lateral":
        trade.mode = "idle"
        trade.exit_price = best_bid if best_bid > 0 else trade.entry_price
        trade.exit_reason = "regime_break"
    elif not signal.get("allow") and str(signal.get("reason") or "") == "no_mid_book_reentry":
        trade.mode = "idle"
        trade.exit_price = best_bid if best_bid > 0 else trade.entry_price
        trade.exit_reason = "edge_faded"

    if trade.mode == "idle" and trade.exit_price is not None and trade.entry_price is not None:
        trade.pnl_ticks = round((trade.exit_price - trade.entry_price) / max(trade.tick_size, 0.001), 4)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trade current scalp mid-book maker setup")
    parser.add_argument("--seconds", type=int, default=900, help="Run duration")
    parser.add_argument("--poll-secs", type=float, default=1.0, help="Polling interval")
    parser.add_argument("--log-file", type=str, default=None, help="Optional JSONL log path")
    args = parser.parse_args()

    cfg = CurrentScalpMidMakerConfigV1()
    research = CurrentScalpMidMakerResearchV1(cfg=cfg)
    trade = MakerPaperTrade()
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    completed: list[dict] = []
    blocked_reasons = Counter()
    fill_reasons = Counter()
    exit_reasons = Counter()

    print("[CURRENT_SCALP_MID_MAKER_CONFIG]")
    pprint(cfg.as_dict())
    print("[LOG_FILE]")
    print(log_path)

    started_at = time.time()
    current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}

    while time.time() - started_at < args.seconds:
        now = time.time()
        slot_bundle = _build_slot_bundle()
        current_item = slot_bundle["queue"].get("current")
        if not current_item:
            print("[CURRENT_SCALP_MID_MAKER] current slot unavailable")
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

        _append_jsonl(
            log_path,
            {
                "type": "snapshot",
                "ts": now,
                "slug": current_slug,
                "secs_to_end": secs_to_end,
                "executable_reason": executable_reason,
                "signal": signal,
                "reference": ref,
                "trade": asdict(trade),
            },
        )

        if trade.mode == "idle":
            if signal.get("allow"):
                side = str(signal.get("side") or "UP")
                tick_size = _tick_size_from_snap(snap, side)
                trade = _paper_place_entry(signal, tick_size, now, snap, cfg)
                _append_jsonl(log_path, {"type": "entry_posted", "ts": now, "signal": signal, "trade": asdict(trade)})
            else:
                blocked_reasons[str(signal.get("reason") or "unknown")] += 1
        elif trade.mode == "pending_entry":
            trade = _maybe_fill_pending_entry(trade, snap=snap, executable=executable, signal=signal, now=now)
            if trade.mode == "open":
                fill_reasons["maker_fill"] += 1
                _append_jsonl(log_path, {"type": "fill", "ts": now, "trade": asdict(trade), "signal": signal})
            elif trade.mode == "idle":
                fill_reasons[str(trade.exit_reason or "missed")] += 1
                _append_jsonl(log_path, {"type": "entry_expired", "ts": now, "trade": asdict(trade), "signal": signal})
                trade = MakerPaperTrade()
        elif trade.mode == "open":
            trade = _manage_open_trade(trade, executable=executable, signal=signal, now=now, secs_to_end=secs_to_end, cfg=cfg)
            if trade.mode == "idle":
                completed.append(asdict(trade))
                exit_reasons[str(trade.exit_reason or "unknown")] += 1
                _append_jsonl(log_path, {"type": "exit", "ts": now, "trade": completed[-1], "signal": signal})
                trade = MakerPaperTrade()

        time.sleep(max(0.5, float(args.poll_secs)))

    if trade.mode == "open":
        trade.mode = "idle"
        trade.exit_price = trade.entry_price
        trade.exit_reason = "session_end"
        trade.pnl_ticks = 0.0
        completed.append(asdict(trade))
        exit_reasons[str(trade.exit_reason or "unknown")] += 1
        _append_jsonl(log_path, {"type": "exit", "ts": time.time(), "trade": completed[-1]})

    summary = {
        "stats": _trade_stats(completed),
        "fill_reasons": dict(fill_reasons),
        "exit_reasons": dict(exit_reasons),
        "top_blocked_reasons": blocked_reasons.most_common(10),
        "log_file": str(log_path),
    }
    _append_jsonl(log_path, {"type": "summary", "ts": time.time(), "summary": summary})
    print("[CURRENT_SCALP_MID_MAKER_SUMMARY]")
    pprint(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
