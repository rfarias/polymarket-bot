from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint
from typing import Any

from analyze_almost_resolved_gray_zone_v1 import _gray_candidate, _green_candidate, _reversal_risk
from diagnostics_current_almost_resolved_paper_v1 import (
    _bid_for_side,
    _safe_float,
    _slot_secs_to_end,
    _tick_size_from_snap,
)
from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.manual_overlay_v1 import ManualOverlayEngineV1
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _fetch_slot_state, _slot_snapshot
from market.slug_discovery import fetch_event_by_slug


@dataclass
class GrayZonePaperTrade:
    mode: str = "idle"
    source: str = ""
    slug: str = ""
    side: str = ""
    entry_price: float | None = None
    target_price: float | None = None
    stop_price: float | None = None
    entry_ts: float = 0.0
    entry_secs: int | None = None
    promoted_to_hold: bool = False
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_ticks: float | None = None


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _ask_for_side_from_signal(signal: dict[str, Any], side: str) -> float:
    return _safe_float(signal.get("up_buy" if side == "UP" else "down_buy"), 0.0)


def _winner_for_side(signal: dict[str, Any], side: str) -> bool:
    signed = _safe_float(signal.get("signed_distance_from_open_bps"), 0.0)
    return (side == "UP" and signed > 0) or (side == "DOWN" and signed < 0)


def _close_trade(trade: GrayZonePaperTrade, *, price: float, reason: str, tick_size: float) -> None:
    trade.mode = "idle"
    trade.exit_price = round(max(0.0, min(1.0, price)), 6)
    trade.exit_reason = reason
    entry = _safe_float(trade.entry_price, 0.0)
    trade.pnl_ticks = round((trade.exit_price - entry) / max(0.001, tick_size), 4)


def _build_default_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / "current_almost_resolved_gray_zone_paper_v1" / f"gray_zone_{ts}.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description="Realtime paper for almost-resolved green hold + gray-zone target/stop")
    parser.add_argument("--seconds", type=int, default=0, help="Run duration. Use 0 to run indefinitely.")
    parser.add_argument("--poll-secs", type=float, default=1.0)
    parser.add_argument("--log-file", type=str, default="")
    parser.add_argument("--gray-min-score", type=int, default=35)
    parser.add_argument("--gray-max-score", type=int, default=84)
    parser.add_argument("--gray-min-distance-usd", type=float, default=40.0)
    parser.add_argument("--gray-min-distance-bps", type=float, default=5.0)
    parser.add_argument("--gray-min-leader-price", type=float, default=0.98)
    parser.add_argument("--gray-max-counter-price", type=float, default=0.03)
    parser.add_argument("--gray-max-secs-to-end", type=int, default=75)
    parser.add_argument("--gray-target-ticks", type=int, default=1)
    parser.add_argument("--gray-stop-ticks", type=int, default=2)
    parser.add_argument("--allow-high-reversal", action="store_true")
    args = parser.parse_args()

    cfg = CurrentAlmostResolvedConfigV1()
    scalp_cfg = CurrentScalpConfigV1()
    scalp = CurrentScalpResearchV1(cfg=scalp_cfg)
    overlay = ManualOverlayEngineV1(scalp_cfg=scalp_cfg, signal_cfg=cfg)
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    trade = GrayZonePaperTrade()
    completed: list[dict[str, Any]] = []
    entries = Counter()
    exits = Counter()
    blocked = Counter()
    current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}
    last_snapshot_signal: dict[str, Any] | None = None
    last_snapshot_slug = ""

    print("[GRAY_ZONE_PAPER_CONFIG]")
    pprint(vars(args))
    print("[LOG_FILE]")
    print(log_path)

    started = time.time()
    while args.seconds <= 0 or time.time() - started < args.seconds:
        now = time.time()
        slot_bundle = _build_slot_bundle()
        current_item = slot_bundle["queue"].get("current")
        if not current_item:
            time.sleep(max(0.5, float(args.poll_secs)))
            continue

        slug = str(current_item.get("slug") or "")
        if slug != current_open_reference.get("slug"):
            raw_event = fetch_event_by_slug(slug)
            market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
            event_start_time = market.get("eventStartTime") or raw_event.get("startTime") if raw_event else None
            open_ref = fetch_binance_open_price_for_event_start_v1(event_start_time)
            current_open_reference = {"slug": slug, "price": open_ref.get("open_price"), "event_start_time": event_start_time}

        slot_state = _fetch_slot_state(slot_bundle)
        snap = _slot_snapshot(slot_state, "current")
        current_exec, current_exec_reason = _compute_executable_metrics(snap)
        secs = _slot_secs_to_end(current_item)
        reference = fetch_external_btc_reference_v1()
        scalp_signal = scalp.evaluate(
            snap=snap,
            secs_to_end=secs,
            event_start_time=current_open_reference.get("event_start_time"),
            now_ts=now,
            reference_price=reference.get("reference_price"),
            source_divergence_bps=reference.get("source_divergence_bps"),
            opening_reference_price=current_open_reference.get("price"),
        )
        signal = dict(evaluate_current_almost_resolved_v1(snap=snap, secs_to_end=secs, reference_signal=scalp_signal, cfg=cfg))
        signal["current_slug"] = slug
        signal["signed_distance_from_open_bps"] = scalp_signal.get("distance_from_open_bps")
        row = {
            "type": "snapshot",
            "ts": now,
            "current_slug": slug,
            "current_secs": secs,
            "reference": reference,
            "current_scalp_context": scalp_signal,
            "signal": signal,
            "trade": asdict(trade),
            "exec_reason": current_exec_reason,
        }
        green, green_side = _green_candidate(row, overlay)
        gray, gray_side, gray_reason = _gray_candidate(
            row=row,
            cfg=cfg,
            engine=overlay,
            min_score=int(args.gray_min_score),
            max_score=int(args.gray_max_score),
            min_distance_usd=float(args.gray_min_distance_usd),
            min_distance_bps=float(args.gray_min_distance_bps),
            min_leader_price=float(args.gray_min_leader_price),
            max_counter_price=float(args.gray_max_counter_price),
            max_secs_to_end=int(args.gray_max_secs_to_end),
            allow_high_reversal=bool(args.allow_high_reversal),
        )
        risk = _reversal_risk(overlay, signal, scalp_signal)
        row["green_hold_ready"] = green
        row["gray_target_stop_ready"] = gray
        row["gray_block_reason"] = gray_reason
        row["reversal_risk"] = risk
        _append_jsonl(log_path, row)

        if trade.mode == "open" and slug != trade.slug:
            tick_size = 0.01
            settle_signal = last_snapshot_signal or signal
            _close_trade(
                trade,
                price=1.0 if _winner_for_side(settle_signal, trade.side) else 0.0,
                reason="resolution_slug_roll",
                tick_size=tick_size,
            )

        if trade.mode == "open":
            tick_size = _tick_size_from_snap(snap, trade.side)
            bid_now = _bid_for_side(current_exec, trade.side)
            if trade.source == "gray_target_stop" and not trade.promoted_to_hold and green and green_side == trade.side:
                trade.promoted_to_hold = True
                _append_jsonl(log_path, {"type": "promote_to_hold", "ts": now, "signal": signal, "trade": asdict(trade)})
            if bid_now > 0 and bid_now <= _safe_float(trade.stop_price, 0.0):
                _close_trade(trade, price=bid_now, reason="stop", tick_size=tick_size)
            elif trade.source == "gray_target_stop" and not trade.promoted_to_hold and bid_now >= _safe_float(trade.target_price, 999.0):
                _close_trade(trade, price=bid_now, reason="gray_target", tick_size=tick_size)
            elif trade.promoted_to_hold and secs is not None and secs <= 1:
                _close_trade(trade, price=1.0 if _winner_for_side(signal, trade.side) else 0.0, reason="resolution", tick_size=tick_size)
            elif trade.source == "gray_target_stop" and not trade.promoted_to_hold and secs is not None and secs <= cfg.min_secs_to_end:
                _close_trade(trade, price=bid_now if bid_now > 0 else _safe_float(trade.entry_price), reason="gray_deadline", tick_size=tick_size)

        if trade.mode == "idle" and trade.exit_reason:
            completed.append(asdict(trade))
            exits[str(trade.exit_reason)] += 1
            _append_jsonl(log_path, {"type": "exit", "ts": now, "trade": asdict(trade)})
            print("[GRAY_ZONE_EXIT]")
            pprint(asdict(trade))
            trade = GrayZonePaperTrade()

        if trade.mode == "idle":
            source = "green_hold" if green else "gray_target_stop" if gray else ""
            side = green_side if green else gray_side
            if source and side in ("UP", "DOWN"):
                ask = _ask_for_side_from_signal(signal, side)
                if ask > 0:
                    tick_size = _tick_size_from_snap(snap, side)
                    trade = GrayZonePaperTrade(
                        mode="open",
                        source=source,
                        slug=slug,
                        side=side,
                        entry_price=round(ask, 6),
                        target_price=1.0 if source == "green_hold" else round(min(0.99, ask + int(args.gray_target_ticks) * tick_size), 6),
                        stop_price=round(max(0.01, ask - (cfg.stop_ticks if source == "green_hold" else int(args.gray_stop_ticks)) * tick_size), 6),
                        entry_ts=now,
                        entry_secs=secs,
                        promoted_to_hold=source == "green_hold",
                    )
                    entries[source] += 1
                    _append_jsonl(log_path, {"type": "enter", "ts": now, "signal": signal, "trade": asdict(trade)})
                    print("[GRAY_ZONE_ENTER]")
                    pprint(asdict(trade))
            else:
                blocked[str(gray_reason or signal.get("reason") or "unknown")] += 1

        last_snapshot_signal = signal
        last_snapshot_slug = slug
        stats = {
            "entries": dict(entries),
            "exits": dict(exits),
            "completed": len(completed),
            "pnl_ticks": round(sum(_safe_float(t.get("pnl_ticks")) for t in completed), 4),
            "blocked_top": blocked.most_common(5),
            "open_trade": asdict(trade),
        }
        print("[GRAY_ZONE_PAPER]", stats)
        time.sleep(max(0.5, float(args.poll_secs)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
