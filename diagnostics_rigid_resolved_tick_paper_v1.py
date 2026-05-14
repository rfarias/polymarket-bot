from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint
from typing import Dict, Optional

from market.current_scalp_signal_v1 import (
    CurrentScalpConfigV1,
    CurrentScalpResearchV1,
    fetch_binance_open_price_for_event_start_v1,
    fetch_external_btc_reference_v1,
)
from market.rest_5m_shadow_public_v5 import _build_slot_bundle, _fetch_slot_state, _slot_snapshot
from market.rest_15m_shadow_public_v1 import build_slot_bundle_15m_v1, fetch_slot_state_15m_v1, slot_snapshot_15m_v1
from market.rigid_resolved_tick_signal_v1 import (
    RigidResolvedTickConfigV1,
    RigidResolvedTickStateV1,
    evaluate_rigid_resolved_tick_v1,
)
from market.slug_discovery import fetch_event_by_slug


@dataclass
class PaperOrder:
    active: bool = False
    slug: Optional[str] = None
    side: Optional[str] = None
    limit_price: Optional[float] = None
    total_qty: float = 50.0
    filled_qty: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    replace_count: int = 0
    size_label: Optional[str] = None


@dataclass
class PaperTrade:
    mode: str = "idle"
    slug: Optional[str] = None
    side: Optional[str] = None
    entry_price: Optional[float] = None
    qty: float = 0.0
    created_at: float = 0.0
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_ticks: Optional[float] = None
    pnl_quote: Optional[float] = None
    size_label: Optional[str] = None


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
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
    return Path("logs") / f"rigid_resolved_tick_paper_{ts}.jsonl"


def _fetch_event_start_time(slug: str) -> Optional[str]:
    raw_event = fetch_event_by_slug(slug)
    market = (raw_event.get("markets") or [{}])[0] if raw_event else {}
    return market.get("eventStartTime") or raw_event.get("startTime") if raw_event else None


def _side_book(snap: Dict, side: str) -> Dict:
    return (snap.get("up") if side == "UP" else snap.get("down")) or {}


def _ask_for_side(snap: Dict, side: str) -> float:
    book = _side_book(snap, side)
    return _safe_float(book.get("executable_buy") or book.get("best_ask"), 0.0)


def _raw_best_ask_for_side(snap: Dict, side: str) -> float:
    top_asks = _side_book(snap, side).get("top_asks") or []
    if top_asks:
        return _safe_float((top_asks[0] or {}).get("price"), 0.0)
    return _ask_for_side(snap, side)


def _bid_for_side(snap: Dict, side: str) -> float:
    book = _side_book(snap, side)
    return _safe_float(book.get("best_bid") or book.get("executable_sell"), 0.0)


def _tick_size(snap: Dict, side: str, default: float = 0.01) -> float:
    return max(0.001, _safe_float(_side_book(snap, side).get("tick_size"), default))


def _available_ask_qty_at_or_below(snap: Dict, side: str, limit_price: float) -> float:
    qty = 0.0
    for level in (_side_book(snap, side).get("top_asks") or []):
        price = _safe_float((level or {}).get("price"), 999.0)
        size = _safe_float((level or {}).get("size"), 0.0)
        if price <= limit_price and size > 0:
            qty += size
        else:
            break
    return round(qty, 6)


def _available_bid_qty_at_or_above(snap: Dict, side: str, limit_price: float) -> float:
    qty = 0.0
    for level in (_side_book(snap, side).get("top_bids") or []):
        price = _safe_float((level or {}).get("price"), -1.0)
        size = _safe_float((level or {}).get("size"), 0.0)
        if price >= limit_price and size > 0:
            qty += size
    return round(qty, 6)


def _apply_fill(existing: PaperTrade, *, side: str, price: float, qty: float, now: float, size_label: Optional[str]) -> PaperTrade:
    if qty <= 0:
        return existing
    if existing.mode != "open":
        return PaperTrade(
            mode="open",
            slug=existing.slug,
            side=side,
            entry_price=price,
            qty=round(qty, 6),
            created_at=now,
            size_label=size_label,
        )
    old_qty = _safe_float(existing.qty, 0.0)
    old_notional = old_qty * _safe_float(existing.entry_price, 0.0)
    new_qty = old_qty + qty
    existing.entry_price = round((old_notional + qty * price) / new_qty, 6) if new_qty > 0 else price
    existing.qty = round(new_qty, 6)
    return existing


def _paper_manage_trade(
    trade: PaperTrade,
    *,
    snap: Dict,
    signal: Dict,
    secs_to_end: Optional[int],
    now: float,
    cfg: RigidResolvedTickConfigV1,
) -> PaperTrade:
    if trade.mode != "open" or not trade.side:
        return trade
    bid = _bid_for_side(snap, trade.side)
    tick = _tick_size(snap, trade.side, cfg.tick_size_default)
    leader = _ask_for_side(snap, trade.side)
    adverse_reason = str(signal.get("reason") or "")

    if bid >= cfg.target_price or leader >= cfg.chase_leader_price:
        trade.mode = "idle"
        trade.exit_price = max(bid, cfg.target_price)
        trade.exit_reason = "resolved_or_target"
    elif bid <= cfg.stop_price or leader < cfg.cancel_if_leader_below:
        trade.mode = "idle"
        trade.exit_price = bid if bid > 0 else leader
        trade.exit_reason = "stop_or_lost_resolved_price"
    elif adverse_reason in ("spot_5s_reversing_against_side", "spot_15s_reversing_against_side", "counter_side_too_expensive"):
        trade.mode = "idle"
        trade.exit_price = bid if bid > 0 else leader
        trade.exit_reason = "context_deteriorated"
    elif secs_to_end is not None and secs_to_end <= 1:
        trade.mode = "idle"
        trade.exit_price = max(bid, leader)
        trade.exit_reason = "deadline"
    elif now - trade.created_at >= 180:
        trade.mode = "idle"
        trade.exit_price = bid if bid > 0 else leader
        trade.exit_reason = "timeout"

    if trade.mode == "idle" and trade.exit_price is not None and trade.entry_price is not None:
        trade.pnl_ticks = round((trade.exit_price - trade.entry_price) / tick, 4)
        trade.pnl_quote = round((trade.exit_price - trade.entry_price) * _safe_float(trade.qty, 0.0), 6)
    return trade


def _trade_stats(completed: list[dict]) -> dict:
    total_pnl_ticks = round(sum(_safe_float(t.get("pnl_ticks")) for t in completed), 4)
    total_pnl_quote = round(sum(_safe_float(t.get("pnl_quote")) for t in completed), 6)
    total_estimated_rebate = round(sum(_safe_float(t.get("estimated_maker_rebate")) for t in completed), 8)
    total_qty = round(sum(_safe_float(t.get("qty")) for t in completed), 6)
    return {
        "completed_trades": len(completed),
        "wins": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) > 0),
        "losses": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) < 0),
        "flat": sum(1 for t in completed if _safe_float(t.get("pnl_ticks")) == 0),
        "total_pnl_ticks": total_pnl_ticks,
        "total_pnl_quote": total_pnl_quote,
        "total_estimated_maker_rebate": total_estimated_rebate,
        "total_filled_qty": total_qty,
    }


def _build_market_context(timeframe: str) -> tuple[dict, dict, dict]:
    if timeframe == "15m":
        bundle = build_slot_bundle_15m_v1()
        item = (bundle.get("queue") or {}).get("current")
        state = fetch_slot_state_15m_v1(bundle)
        snap = slot_snapshot_15m_v1(state, "current")
        return bundle, item, snap
    bundle = _build_slot_bundle()
    item = (bundle.get("queue") or {}).get("current")
    state = _fetch_slot_state(bundle)
    snap = _slot_snapshot(state, "current")
    return bundle, item, snap


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-test the rigid resolved tick method on BTC 5m/15m current markets")
    parser.add_argument("--seconds", type=int, default=600, help="Run duration. Use 0 to run indefinitely.")
    parser.add_argument("--poll-secs", type=float, default=2.0, help="Polling interval")
    parser.add_argument("--timeframes", default="5m,15m", help="Comma-separated list: 5m,15m")
    parser.add_argument("--order-qty", type=float, default=50.0, help="Paper order size in contracts")
    parser.add_argument("--maker-rebate-bps", type=float, default=0.0, help="Estimated maker rebate in bps of notional")
    parser.add_argument("--log-file", type=str, default=None, help="Optional JSONL log path")
    args = parser.parse_args()

    cfg = RigidResolvedTickConfigV1()
    scalp_by_tf = {
        "5m": CurrentScalpResearchV1(cfg=CurrentScalpConfigV1(), history_secs=120),
        "15m": CurrentScalpResearchV1(cfg=CurrentScalpConfigV1(), history_secs=180),
    }
    signal_state_by_tf = {tf: RigidResolvedTickStateV1() for tf in ("5m", "15m")}
    order_by_tf = {tf: PaperOrder() for tf in ("5m", "15m")}
    trade_by_tf = {tf: PaperTrade() for tf in ("5m", "15m")}
    open_reference_by_tf: dict[str, dict[str, object | None]] = {
        "5m": {"slug": None, "price": None, "event_start_time": None},
        "15m": {"slug": None, "price": None, "event_start_time": None},
    }
    timeframes = [x.strip() for x in str(args.timeframes).split(",") if x.strip() in ("5m", "15m")]
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    completed: list[dict] = []
    action_counts = Counter()
    reason_counts = Counter()

    print("[RIGID_RESOLVED_TICK_CONFIG]")
    pprint(cfg.as_dict())
    print("[TIMEFRAMES]", timeframes)
    print("[LOG_FILE]", log_path)

    started_at = time.time()
    while args.seconds <= 0 or time.time() - started_at < args.seconds:
        now = time.time()
        reference = fetch_external_btc_reference_v1()

        for timeframe in timeframes:
            item = None
            try:
                _, item, snap = _build_market_context(timeframe)
                if not item:
                    print(f"[{timeframe}] current slot unavailable")
                    continue

                slug = str(item.get("slug") or "")
                if slug != open_reference_by_tf[timeframe].get("slug"):
                    event_start_time = _fetch_event_start_time(slug)
                    open_ref = fetch_binance_open_price_for_event_start_v1(event_start_time)
                    open_reference_by_tf[timeframe] = {
                        "slug": slug,
                        "price": open_ref.get("open_price"),
                        "event_start_time": event_start_time,
                    }

                secs_to_end = _slot_secs_to_end(item)
                scalp_signal = scalp_by_tf[timeframe].evaluate(
                    snap=snap,
                    secs_to_end=secs_to_end,
                    event_start_time=open_reference_by_tf[timeframe].get("event_start_time"),
                    now_ts=now,
                    reference_price=reference.get("reference_price"),
                    source_divergence_bps=reference.get("source_divergence_bps"),
                    opening_reference_price=open_reference_by_tf[timeframe].get("price"),
                )
                signal = evaluate_rigid_resolved_tick_v1(
                    snap=snap,
                    secs_to_end=secs_to_end,
                    reference_signal=scalp_signal,
                    state=signal_state_by_tf[timeframe],
                    slot_key=slug,
                    timeframe=timeframe,
                    cfg=cfg,
                )
                action_counts[str(signal.get("action") or "WAIT")] += 1
                reason_counts[str(signal.get("reason") or "unknown")] += 1

                order = order_by_tf[timeframe]
                trade = trade_by_tf[timeframe]
                if order.active and order.slug and order.slug != slug:
                    _append_jsonl(
                        log_path,
                        {
                            "type": "cancel",
                            "ts": now,
                            "timeframe": timeframe,
                            "old_slug": order.slug,
                            "new_slug": slug,
                            "reason": "slot_changed_cancel_stale_order",
                            "order": asdict(order),
                        },
                    )
                    order = PaperOrder(total_qty=float(args.order_qty))
                if trade.mode == "open" and trade.slug and trade.slug != slug:
                    trade.exit_price = _bid_for_side(snap, trade.side or "UP")
                    trade.exit_reason = "slot_changed_deadline"
                    tick = _tick_size(snap, trade.side or "UP", cfg.tick_size_default)
                    trade.pnl_ticks = round((_safe_float(trade.exit_price) - _safe_float(trade.entry_price)) / tick, 4)
                    trade.pnl_quote = round((_safe_float(trade.exit_price) - _safe_float(trade.entry_price)) * _safe_float(trade.qty), 6)
                    row = asdict(trade)
                    row["timeframe"] = timeframe
                    completed.append(row)
                    _append_jsonl(log_path, {"type": "exit", "ts": now, "trade": row})
                    trade = PaperTrade()
                action = str(signal.get("action") or "WAIT")
                status = str(signal.get("status") or "WAIT")
                side = str(signal.get("side") or "")
                limit_price = _safe_float(signal.get("limit_price"), 0.0)
                order_created_this_tick = False

                if trade.mode == "idle" and action == "PLACE_LIMIT" and side in ("UP", "DOWN") and limit_price > 0:
                    raw_best_ask = _raw_best_ask_for_side(snap, side)
                    if raw_best_ask <= limit_price:
                        _append_jsonl(
                            log_path,
                            {
                                "type": "cancel",
                                "ts": now,
                                "timeframe": timeframe,
                                "slug": slug,
                                "reason": "post_only_would_cross",
                                "side": side,
                                "limit_price": limit_price,
                                "raw_best_ask": raw_best_ask,
                                "signal": signal,
                            },
                        )
                    else:
                        order_created_this_tick = True
                        order.active = True
                        order.slug = slug
                        order.side = side
                        order.limit_price = limit_price
                        order.total_qty = float(args.order_qty)
                        order.filled_qty = 0.0
                        order.created_at = now
                        order.updated_at = now
                        order.size_label = str(signal.get("size_label") or "full")
                elif action == "CANCEL_REPLACE" and order.active and side in ("UP", "DOWN") and limit_price > 0:
                    raw_best_ask = _raw_best_ask_for_side(snap, side)
                    if raw_best_ask <= limit_price:
                        order = PaperOrder(total_qty=float(args.order_qty))
                        _append_jsonl(
                            log_path,
                            {
                                "type": "cancel",
                                "ts": now,
                                "timeframe": timeframe,
                                "slug": slug,
                                "reason": "post_only_replace_would_cross",
                                "side": side,
                                "limit_price": limit_price,
                                "raw_best_ask": raw_best_ask,
                                "signal": signal,
                            },
                        )
                    else:
                        order_created_this_tick = True
                        remaining_qty = max(0.0, _safe_float(order.total_qty, float(args.order_qty)) - _safe_float(order.filled_qty, 0.0))
                        order.active = True
                        order.slug = slug
                        order.side = side
                        order.limit_price = limit_price
                        order.total_qty = remaining_qty if remaining_qty > 0 else float(args.order_qty)
                        order.filled_qty = 0.0
                        order.updated_at = now
                        order.replace_count += 1
                elif action == "CANCEL":
                    order = PaperOrder(total_qty=float(args.order_qty))

                if order.active and order.side and action != "CANCEL" and not order_created_this_tick:
                    if order.limit_price is not None:
                        remaining_qty = max(0.0, _safe_float(order.total_qty, float(args.order_qty)) - _safe_float(order.filled_qty, 0.0))
                        available_qty = _available_ask_qty_at_or_below(snap, order.side, order.limit_price)
                        fill_qty = round(min(remaining_qty, available_qty), 6)
                    else:
                        remaining_qty = 0.0
                        available_qty = 0.0
                        fill_qty = 0.0
                    if fill_qty > 0 and order.limit_price is not None:
                        order.filled_qty = round(_safe_float(order.filled_qty, 0.0) + fill_qty, 6)
                        trade = _apply_fill(
                            trade,
                            side=order.side,
                            price=order.limit_price,
                            qty=fill_qty,
                            now=now,
                            size_label=order.size_label,
                        )
                        trade.slug = slug
                        _append_jsonl(
                            log_path,
                            {
                                "type": "fill",
                                "ts": now,
                                "timeframe": timeframe,
                                "slug": slug,
                                "fill_qty": fill_qty,
                                "estimated_maker_rebate": round(fill_qty * order.limit_price * float(args.maker_rebate_bps) / 10000.0, 8),
                                "available_qty_at_limit": available_qty,
                                "remaining_qty_before_fill": remaining_qty,
                                "order": asdict(order),
                                "trade": asdict(trade),
                                "signal": signal,
                            },
                        )
                        print(
                            f"[{timeframe}] PAPER_FILL {trade.side} qty={fill_qty}/{args.order_qty} "
                            f"@ {order.limit_price} avg={trade.entry_price} open_qty={trade.qty}"
                        )
                        if order.filled_qty >= order.total_qty:
                            order = PaperOrder(total_qty=float(args.order_qty))

                if trade.mode == "open":
                    trade = _paper_manage_trade(
                        trade,
                        snap=snap,
                        signal=signal,
                        secs_to_end=secs_to_end,
                        now=now,
                        cfg=cfg,
                    )
                    if trade.mode == "idle" and trade.exit_reason:
                        row = asdict(trade)
                        row["timeframe"] = timeframe
                        row["slug"] = slug
                        row["estimated_maker_rebate"] = round(
                            _safe_float(row.get("qty")) * _safe_float(row.get("entry_price")) * float(args.maker_rebate_bps) / 10000.0,
                            8,
                        )
                        completed.append(row)
                        _append_jsonl(log_path, {"type": "exit", "ts": now, "trade": row})
                        print(f"[{timeframe}] PAPER_EXIT {row}")
                        trade = PaperTrade()

                order_by_tf[timeframe] = order
                trade_by_tf[timeframe] = trade

                print(
                    f"[{timeframe}] secs={secs_to_end} status={status} action={signal.get('action')} "
                    f"score={signal.get('score')} phase={signal.get('phase')} "
                    f"side={signal.get('side')} leader={signal.get('leader_price')} limit={signal.get('limit_price')} "
                    f"reason={signal.get('reason')} order={asdict(order)} trade={asdict(trade)}"
                )
                _append_jsonl(
                    log_path,
                    {
                        "type": "snapshot",
                        "ts": now,
                        "timeframe": timeframe,
                        "slug": slug,
                        "secs_to_end": secs_to_end,
                        "reference": reference,
                        "open_reference": open_reference_by_tf[timeframe],
                        "scalp_context": scalp_signal,
                        "signal": signal,
                        "order": asdict(order),
                        "trade": asdict(trade),
                    },
                )
            except Exception as exc:
                print(f"[{timeframe}] ERROR {type(exc).__name__}: {exc}")
                _append_jsonl(
                    log_path,
                    {
                        "type": "error",
                        "ts": now,
                        "timeframe": timeframe,
                        "item": item,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )

        time.sleep(max(0.5, float(args.poll_secs)))

    print("\n[SUMMARY]")
    pprint(
        {
            "stats": _trade_stats(completed),
            "action_counts": dict(action_counts),
            "reason_counts_top10": reason_counts.most_common(10),
            "log_file": str(log_path),
            "trades": completed,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
