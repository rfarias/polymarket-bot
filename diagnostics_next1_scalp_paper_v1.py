from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint

from market.current_scalp_signal_v1 import fetch_external_btc_reference_v1
from market.next1_scalp_signal_v1 import Next1ScalpConfigV1, Next1ScalpResearchV1
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _current_secs_to_end, _fetch_slot_state, _slot_snapshot


@dataclass
class PaperTrade:
    mode: str = "idle"  # idle | open
    side: str | None = None
    setup: str | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    best_bid: float | None = None
    created_at: float = 0.0
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_ticks: float | None = None


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _tick_size_from_snap(snap: dict, side: str) -> float:
    book_side = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(book_side.get("tick_size"), 0.01))


def _bid_for_side(executable: dict | None, side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _paper_enter(signal: dict, tick_size: float, now: float, cfg: Next1ScalpConfigV1) -> PaperTrade:
    trade = PaperTrade()
    trade.mode = "open"
    trade.side = signal.get("side")
    trade.setup = signal.get("setup")
    trade.entry_price = _safe_float(signal.get("entry_price"), 0.0)
    trade.stop_price = round(max(0.01, trade.entry_price - cfg.stop_ticks * tick_size), 6)
    trade.target_price = round(min(0.99, trade.entry_price + cfg.target_ticks * tick_size), 6)
    trade.created_at = now
    return trade


def _paper_manage(trade: PaperTrade, *, bid_now: float, tick_size: float, now: float, next1_secs: int | None, cfg: Next1ScalpConfigV1) -> PaperTrade:
    if trade.mode != "open":
        return trade
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
    elif next1_secs is not None and next1_secs <= cfg.min_secs_to_end:
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "deadline"
    elif now - trade.created_at >= cfg.max_hold_secs:
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "timeout"

    if trade.mode == "idle" and trade.exit_price is not None and trade.entry_price is not None:
        trade.pnl_ticks = round((trade.exit_price - trade.entry_price) / tick_size, 4)
    return trade


def _build_default_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"next1_scalp_paper_{ts}.jsonl"


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _setup_stats(trades: list[dict]) -> dict:
    stats: dict = {}
    for trade in trades:
        setup = str(trade.get("setup") or "unknown")
        bucket = stats.setdefault(
            setup,
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
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trade next1 scalp using current + spot context")
    parser.add_argument("--seconds", type=int, default=60, help="Run duration")
    parser.add_argument("--poll-secs", type=float, default=2.0, help="Polling interval")
    parser.add_argument("--log-file", type=str, default=None, help="Optional JSONL log path")
    args = parser.parse_args()

    cfg = Next1ScalpConfigV1()
    research = Next1ScalpResearchV1(cfg=cfg)
    trade = PaperTrade()
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()

    print("[NEXT1_SCALP_CONFIG]")
    pprint(cfg.as_dict())
    print("[LOG_FILE]")
    print(log_path)

    slot_bundle = _build_slot_bundle()
    started_at = time.time()
    completed: list[dict] = []

    while time.time() - started_at < args.seconds:
        now = time.time()
        slot_state = _fetch_slot_state(slot_bundle)
        current_item = slot_bundle["queue"].get("current")
        next1_item = slot_bundle["queue"].get("next_1")
        current_secs = _current_secs_to_end(current_item.get("seconds_to_end") if current_item else None, started_at)
        next1_secs = _current_secs_to_end(next1_item.get("seconds_to_end") if next1_item else None, started_at)
        current_snap = _slot_snapshot(slot_state, "current")
        next1_snap = _slot_snapshot(slot_state, "next_1")
        ref = fetch_external_btc_reference_v1()
        signal = research.evaluate(
            current_snap=current_snap,
            next1_snap=next1_snap,
            current_secs=current_secs,
            next1_secs=next1_secs,
            reference_price=ref.get("reference_price"),
            source_divergence_bps=ref.get("source_divergence_bps"),
            now_ts=now,
        )
        next1_exec, next1_exec_reason = _compute_executable_metrics(next1_snap)

        print("\n===== NEXT1 SCALP PAPER V1 =====")
        print(
            f"current_secs={current_secs} next1_secs={next1_secs} exec_reason={next1_exec_reason} "
            f"setup={signal.get('setup')} allow={signal.get('allow')} side={signal.get('side')} trade_mode={trade.mode}"
        )
        print("[REFERENCE]")
        pprint(ref)
        print("[SIGNAL]")
        pprint(signal)
        _append_jsonl(
            log_path,
            {
                "type": "snapshot",
                "ts": now,
                "current_secs": current_secs,
                "next1_secs": next1_secs,
                "reference": ref,
                "signal": signal,
                "trade": asdict(trade),
            },
        )

        if trade.mode == "idle" and signal.get("allow"):
            tick_size = _tick_size_from_snap(next1_snap, signal.get("side"))
            trade = _paper_enter(signal, tick_size, now, cfg)
            print("[PAPER_ENTER]")
            pprint(asdict(trade))
            _append_jsonl(
                log_path,
                {
                    "type": "enter",
                    "ts": now,
                    "signal": signal,
                    "trade": asdict(trade),
                },
            )

        if trade.mode == "open":
            tick_size = _tick_size_from_snap(next1_snap, trade.side or "UP")
            bid_now = _bid_for_side(next1_exec, trade.side or "UP")
            trade = _paper_manage(trade, bid_now=bid_now, tick_size=tick_size, now=now, next1_secs=next1_secs, cfg=cfg)
            print("[PAPER_MANAGE]")
            pprint(asdict(trade))
            if trade.mode == "idle" and trade.exit_reason is not None:
                completed.append(asdict(trade))
                print("[PAPER_EXIT]")
                pprint(completed[-1])
                _append_jsonl(
                    log_path,
                    {
                        "type": "exit",
                        "ts": now,
                        "trade": completed[-1],
                    },
                )
                trade = PaperTrade()

        time.sleep(max(0.5, float(args.poll_secs)))

    print("\n[SUMMARY]")
    pprint(
        {
            "completed_trades": len(completed),
            "wins": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) > 0),
            "losses": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) < 0),
            "flat": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) == 0),
            "total_pnl_ticks": round(sum(_safe_float(t.get("pnl_ticks")) for t in completed), 4),
            "by_setup": _setup_stats(completed),
            "log_file": str(log_path),
            "trades": completed,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
