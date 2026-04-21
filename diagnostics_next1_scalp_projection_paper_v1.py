from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint
from typing import Dict, Optional

from market.current_scalp_signal_v1 import fetch_external_btc_reference_v1
from market.next1_scalp_signal_v1 import Next1ScalpConfigV1, Next1ScalpResearchV1
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _compute_executable_metrics, _fetch_slot_state, _slot_snapshot


SIZES = (20, 50, 100)


@dataclass
class ProjectedTrade:
    size_total: int
    mode: str = "idle"
    side: Optional[str] = None
    setup: Optional[str] = None
    aggressive_qty: int = 0
    passive_qty: int = 0
    aggressive_entry_price: Optional[float] = None
    passive_entry_price: Optional[float] = None
    aggressive_filled: bool = False
    passive_filled: bool = False
    aggressive_touch_count: int = 0
    passive_touch_count: int = 0
    last_entry_reprice_at: float = 0.0
    entry_price_avg: Optional[float] = None
    best_bid: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    created_at: float = 0.0
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_ticks: Optional[float] = None
    pnl_usd: Optional[float] = None


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_log_path() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"next1_scalp_projection_{ts}.jsonl"


def _tick_size_from_snap(snap: dict, side: str) -> float:
    book_side = (snap.get("up") if side == "UP" else snap.get("down")) or {}
    return max(0.001, _safe_float(book_side.get("tick_size"), 0.01))


def _adaptive_passive_entry_price(aggressive_price: float, ask_now: float, tick_size: float) -> float:
    if aggressive_price <= 0:
        return 0.0
    spread = max(0.0, float(ask_now) - float(aggressive_price))
    if spread <= tick_size:
        return round(float(aggressive_price), 6)
    return max(0.01, round(float(aggressive_price) - tick_size, 6))


def _bid_for_side(executable: Optional[dict], side: str) -> float:
    if not executable:
        return 0.0
    return _safe_float(executable.get("up_bid" if side == "UP" else "down_bid"), 0.0)


def _filled_qty(trade: ProjectedTrade) -> int:
    qty = trade.aggressive_qty if trade.aggressive_filled else 0
    if trade.passive_filled:
        qty += trade.passive_qty
    return qty


def _avg_entry(trade: ProjectedTrade) -> Optional[float]:
    gross = 0.0
    qty = 0
    if trade.aggressive_filled and trade.aggressive_entry_price is not None:
        gross += float(trade.aggressive_entry_price) * trade.aggressive_qty
        qty += trade.aggressive_qty
    if trade.passive_filled and trade.passive_entry_price is not None:
        gross += float(trade.passive_entry_price) * trade.passive_qty
        qty += trade.passive_qty
    if qty <= 0:
        return None
    return round(gross / qty, 6)


def _rebuild_levels(trade: ProjectedTrade, *, tick_size: float, cfg: Next1ScalpConfigV1) -> ProjectedTrade:
    trade.entry_price_avg = _avg_entry(trade)
    if trade.entry_price_avg is None:
        return trade
    trade.stop_price = round(max(0.01, float(trade.entry_price_avg) - cfg.stop_ticks * tick_size), 6)
    trade.target_price = round(min(0.99, float(trade.entry_price_avg) + cfg.target_ticks * tick_size), 6)
    return trade


def _enter_trade(signal: dict, *, size_total: int, tick_size: float, now: float, cfg: Next1ScalpConfigV1) -> ProjectedTrade:
    aggressive_qty = size_total // 2
    passive_qty = size_total - aggressive_qty
    trade = ProjectedTrade(
        size_total=size_total,
        mode="open",
        side=signal.get("side"),
        setup=signal.get("setup"),
        aggressive_qty=aggressive_qty,
        passive_qty=passive_qty,
        aggressive_entry_price=_safe_float(signal.get("aggressive_entry_price")),
        passive_entry_price=_safe_float(signal.get("entry_price")),
        created_at=now,
    )
    trade = _rebuild_levels(trade, tick_size=tick_size, cfg=cfg)
    return trade


def _manage_trade(
    trade: ProjectedTrade,
    *,
    signal: dict,
    executable: Optional[dict],
    tick_size: float,
    now: float,
    next1_secs: Optional[int],
    cfg: Next1ScalpConfigV1,
) -> ProjectedTrade:
    if trade.mode != "open":
        return trade

    ask_now = _safe_float(executable.get("up_ask" if trade.side == "UP" else "down_ask"), 0.0) if executable else 0.0
    if (
        signal.get("allow")
        and signal.get("side") == trade.side
        and now - trade.last_entry_reprice_at >= 1.0
        and ask_now > 0
    ):
        trade.aggressive_entry_price = min(ask_now, _safe_float(signal.get("aggressive_entry_price"), ask_now))
        trade.passive_entry_price = _adaptive_passive_entry_price(float(trade.aggressive_entry_price), ask_now, tick_size)
        trade.last_entry_reprice_at = now

    if (
        not trade.aggressive_filled
        and trade.aggressive_qty > 0
        and trade.aggressive_entry_price is not None
        and ask_now > 0
    ):
        if ask_now < float(trade.aggressive_entry_price):
            trade.aggressive_filled = True
        elif ask_now <= float(trade.aggressive_entry_price):
            trade.aggressive_touch_count += 1
            if trade.aggressive_touch_count >= 1 and now - trade.created_at >= 1.0:
                trade.aggressive_filled = True
        if trade.aggressive_filled:
            trade = _rebuild_levels(trade, tick_size=tick_size, cfg=cfg)

    if (
        not trade.passive_filled
        and trade.passive_qty > 0
        and trade.passive_entry_price is not None
        and ask_now > 0
    ):
        if ask_now < float(trade.passive_entry_price):
            trade.passive_filled = True
        elif ask_now <= float(trade.passive_entry_price):
            trade.passive_touch_count += 1
            if trade.passive_touch_count >= 2 and now - trade.created_at >= 3.0:
                trade.passive_filled = True
        if trade.passive_filled:
            trade = _rebuild_levels(trade, tick_size=tick_size, cfg=cfg)

    bid_now = _bid_for_side(executable, trade.side or "UP")
    trade.best_bid = max(_safe_float(trade.best_bid, 0.0), bid_now)
    if trade.entry_price_avg is not None and trade.best_bid >= round(float(trade.entry_price_avg) + tick_size, 6):
        trade.stop_price = max(_safe_float(trade.stop_price), round(trade.best_bid - tick_size, 6))

    passive_expired = (
        not trade.passive_filled
        and (
            not signal.get("allow")
            or signal.get("side") != trade.side
            or (next1_secs is not None and next1_secs < cfg.min_secs_to_end)
            or now - trade.created_at >= 25
        )
    )
    if passive_expired:
        trade.passive_qty = 0
        trade.passive_entry_price = None

    aggressive_expired = (
        not trade.aggressive_filled
        and (
            not signal.get("allow")
            or signal.get("side") != trade.side
            or (next1_secs is not None and next1_secs < cfg.min_secs_to_end)
            or now - trade.created_at >= 25
        )
    )
    if aggressive_expired:
        trade.aggressive_qty = 0
        trade.aggressive_entry_price = None

    if _filled_qty(trade) <= 0 and trade.aggressive_qty <= 0 and trade.passive_qty <= 0:
        trade.mode = "idle"
        trade.exit_reason = "entry_expired"
        return trade

    if trade.entry_price_avg is None:
        return trade

    if bid_now >= _safe_float(trade.target_price):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "target"
    elif bid_now <= _safe_float(trade.stop_price):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "stop"
    elif next1_secs is not None and next1_secs <= 5:
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "deadline"
    elif now - trade.created_at >= cfg.max_hold_secs:
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "timeout"

    if trade.mode == "idle" and trade.entry_price_avg is not None and trade.exit_price is not None:
        trade.pnl_ticks = round((float(trade.exit_price) - float(trade.entry_price_avg)) / tick_size, 4)
        trade.pnl_usd = round((float(trade.exit_price) - float(trade.entry_price_avg)) * _filled_qty(trade), 4)
    return trade


def _summary(completed: list[dict]) -> dict:
    by_size: Dict[int, dict] = {}
    for trade in completed:
        size = int(trade.get("size_total") or 0)
        bucket = by_size.setdefault(size, {"count": 0, "wins": 0, "losses": 0, "flat": 0, "total_pnl_usd": 0.0, "total_pnl_ticks": 0.0})
        pnl_usd = _safe_float(trade.get("pnl_usd"))
        pnl_ticks = _safe_float(trade.get("pnl_ticks"))
        bucket["count"] += 1
        bucket["total_pnl_usd"] = round(bucket["total_pnl_usd"] + pnl_usd, 4)
        bucket["total_pnl_ticks"] = round(bucket["total_pnl_ticks"] + pnl_ticks, 4)
        if pnl_usd > 0:
            bucket["wins"] += 1
        elif pnl_usd < 0:
            bucket["losses"] += 1
        else:
            bucket["flat"] += 1
    return by_size


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live paper projection for next1 scalp at multiple hand sizes")
    parser.add_argument("--seconds", type=int, default=14400)
    parser.add_argument("--poll-secs", type=float, default=2.0)
    parser.add_argument("--log-file", type=str, default=None)
    args = parser.parse_args()

    cfg = Next1ScalpConfigV1()
    research = Next1ScalpResearchV1(cfg=cfg)
    trades = {size: ProjectedTrade(size_total=size) for size in SIZES}
    completed: list[dict] = []
    log_path = Path(args.log_file) if args.log_file else _build_log_path()

    print("[NEXT1_SCALP_PROJECTION_CONFIG]")
    pprint({"cfg": cfg.as_dict(), "sizes": list(SIZES), "log_file": str(log_path)})

    started_at = time.time()
    while time.time() - started_at < args.seconds:
        now = time.time()
        slot_bundle = _build_slot_bundle()
        current_item = slot_bundle["queue"].get("current")
        next1_item = slot_bundle["queue"].get("next_1")
        current_secs = int(current_item.get("seconds_to_end")) if current_item and current_item.get("seconds_to_end") is not None else None
        next1_secs = int(next1_item.get("seconds_to_end")) if next1_item and next1_item.get("seconds_to_end") is not None else None
        slot_state = _fetch_slot_state(slot_bundle)
        current_snap = _slot_snapshot(slot_state, "current")
        next1_snap = _slot_snapshot(slot_state, "next_1")
        next1_exec, next1_exec_reason = _compute_executable_metrics(next1_snap)
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

        _append_jsonl(
            log_path,
            {
                "type": "snapshot",
                "ts": now,
                "current_secs": current_secs,
                "next1_secs": next1_secs,
                "next1_exec_reason": next1_exec_reason,
                "reference": ref,
                "signal": signal,
                "trades": {size: asdict(trade) for size, trade in trades.items()},
            },
        )

        for size, trade in list(trades.items()):
            side_hint = signal.get("side") or trade.side or "UP"
            tick_size = _tick_size_from_snap(next1_snap, side_hint)
            if trade.mode == "idle" and signal.get("allow"):
                trades[size] = _enter_trade(signal, size_total=size, tick_size=tick_size, now=now, cfg=cfg)
                _append_jsonl(log_path, {"type": "enter", "ts": now, "size_total": size, "signal": signal, "trade": asdict(trades[size])})
                continue

            trades[size] = _manage_trade(
                trade,
                signal=signal,
                executable=next1_exec,
                tick_size=tick_size,
                now=now,
                next1_secs=next1_secs,
                cfg=cfg,
            )
            if trade.mode == "open" and trades[size].passive_filled and not trade.passive_filled:
                _append_jsonl(log_path, {"type": "add_fill", "ts": now, "size_total": size, "trade": asdict(trades[size])})
            if trades[size].mode == "idle" and trades[size].exit_reason is not None:
                completed.append(asdict(trades[size]))
                _append_jsonl(log_path, {"type": "exit", "ts": now, "size_total": size, "trade": completed[-1]})
                trades[size] = ProjectedTrade(size_total=size)

        time.sleep(max(0.5, float(args.poll_secs)))

    print("[SUMMARY]")
    pprint({"completed_trades": len(completed), "by_size": _summary(completed), "log_file": str(log_path)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
