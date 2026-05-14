from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from pprint import pprint

from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, evaluate_current_almost_resolved_v1
from market.manual_overlay_v1 import ManualOverlayEngineV1
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
    qty: float = 0.0
    stop_price: float | None = None
    target_price: float | None = None
    best_bid: float | None = None
    created_at: float = 0.0
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_ticks: float | None = None
    pnl_quote: float | None = None
    estimated_maker_rebate: float = 0.0
    hold_to_resolution: bool = False
    setup_variant: str | None = None
    entry_buffer_bps: float | None = None
    entry_distance_bps: float | None = None


@dataclass
class PaperOrder:
    active: bool = False
    slug: str | None = None
    side: str | None = None
    limit_price: float | None = None
    order_style: str = "passive_limit"  # passive_limit | aggressive_limit
    total_qty: float = 50.0
    filled_qty: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0


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


def _side_book(snap: dict, side: str) -> dict:
    return (snap.get("up") if side == "UP" else snap.get("down")) or {}


def _first_level_price(book: dict, side: str, default: float = 0.0) -> float:
    levels = book.get(side) or []
    if not levels:
        return float(default)
    return _safe_float((levels[0] or {}).get("price"), default)


def _ask_for_side(snap: dict, side: str) -> float:
    book = _side_book(snap, side)
    executable_buy = _safe_float(book.get("executable_buy"), 0.0)
    if executable_buy > 0:
        return executable_buy
    best_ask = _safe_float(book.get("best_ask"), 0.0)
    if best_ask > 0:
        return best_ask
    return _first_level_price(book, "top_asks", 0.0)


def _available_ask_qty_at_or_below(snap: dict, side: str, limit_price: float) -> float:
    executable_buy = _ask_for_side(snap, side)
    if executable_buy > 0 and executable_buy <= limit_price:
        return 1_000_000_000.0
    qty = 0.0
    for level in (_side_book(snap, side).get("top_asks") or []):
        price = _safe_float((level or {}).get("price"), 999.0)
        size = _safe_float((level or {}).get("size"), 0.0)
        if price <= limit_price and size > 0:
            qty += size
        else:
            break
    return round(qty, 6)


def _paper_enter(
    signal: dict,
    tick_size: float,
    now: float,
    cfg: CurrentAlmostResolvedConfigV1,
    qty: float = 0.0,
    maker_rebate_bps: float = 0.0,
) -> PaperTrade:
    trade = PaperTrade()
    trade.mode = "open"
    trade.side = signal.get("side")
    trade.entry_price = _safe_float(signal.get("entry_price"), 0.0)
    trade.qty = round(max(0.0, qty), 6)
    trade.target_price = round(min(0.99, _safe_float(signal.get("exit_price"), 0.99)), 6)
    explicit_stop = _safe_float(signal.get("stop_price"), 0.0)
    if explicit_stop > 0:
        trade.stop_price = round(max(0.01, explicit_stop), 6)
    else:
        trade.stop_price = round(max(0.01, trade.entry_price - cfg.stop_ticks * tick_size), 6)
    trade.created_at = now
    trade.setup_variant = str(signal.get("setup_variant") or "standard")
    trade.entry_buffer_bps = _safe_float(
        signal.get("up_price_to_beat_buffer_bps" if trade.side == "UP" else "down_price_to_beat_buffer_bps"),
        0.0,
    )
    trade.entry_distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    trade.estimated_maker_rebate = round(trade.qty * trade.entry_price * maker_rebate_bps / 10000.0, 8)
    return trade


def _apply_passive_fill(
    trade: PaperTrade,
    *,
    signal: dict,
    fill_price: float,
    fill_qty: float,
    tick_size: float,
    now: float,
    cfg: CurrentAlmostResolvedConfigV1,
    maker_rebate_bps: float,
) -> PaperTrade:
    if fill_qty <= 0:
        return trade
    if trade.mode != "open":
        filled_signal = dict(signal)
        filled_signal["entry_price"] = fill_price
        return _paper_enter(filled_signal, tick_size, now, cfg, qty=fill_qty, maker_rebate_bps=maker_rebate_bps)
    old_qty = _safe_float(trade.qty, 0.0)
    old_notional = old_qty * _safe_float(trade.entry_price, 0.0)
    new_qty = old_qty + fill_qty
    trade.entry_price = round((old_notional + fill_qty * fill_price) / new_qty, 6) if new_qty > 0 else fill_price
    trade.qty = round(new_qty, 6)
    trade.estimated_maker_rebate = round(
        _safe_float(trade.estimated_maker_rebate, 0.0) + fill_qty * fill_price * maker_rebate_bps / 10000.0,
        8,
    )
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
    hold_winner_to_resolution: bool = False,
    resolution_settle_secs: int = 1,
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
    market_range_15s = _safe_float(signal.get("market_range_15s"), 0.0)
    setup_variant = str(trade.setup_variant or signal.get("setup_variant") or "standard")
    entry_buffer_bps = _safe_float(trade.entry_buffer_bps, buffer_bps)
    entry_distance_bps = _safe_float(trade.entry_distance_bps, open_distance_bps)
    signed_distance_from_open_bps = _safe_float(signal.get("signed_distance_from_open_bps"), 0.0)
    side_winning = (side == "UP" and signed_distance_from_open_bps > 0) or (
        side == "DOWN" and signed_distance_from_open_bps < 0
    )
    resolved_pullback_late_hold = (
        setup_variant == "resolved_pullback_limit"
        and secs_to_end is not None
        and secs_to_end <= cfg.resolved_pullback_preferred_secs
    )
    resolved_pullback_early_target_ok = (
        setup_variant == "resolved_pullback_limit"
        and secs_to_end is not None
        and secs_to_end > cfg.resolved_pullback_preferred_secs
    )

    if (
        secs_to_end is not None
        and secs_to_end <= cfg.paper_hold_to_resolution_secs
        and bid_now >= cfg.paper_hold_to_resolution_min_price
        and buffer_bps >= cfg.paper_hold_to_resolution_min_buffer_bps
        and open_distance_bps >= cfg.paper_hold_to_resolution_min_open_distance_bps
        and market_range_30s <= cfg.paper_profit_take_on_market_range_30s
    ):
        trade.hold_to_resolution = True

    if (
        setup_variant == "controlled_late_entry"
        and pnl_ticks_now > 0
        and (
            market_range_15s >= cfg.controlled_late_max_market_range_15s
            or adverse_spot_bps >= cfg.controlled_late_max_adverse_spot_15s_bps
            or buffer_bps <= max(cfg.paper_structural_stop_buffer_bps, entry_buffer_bps * 0.7)
            or open_distance_bps <= max(cfg.min_price_to_beat_distance_bps, entry_distance_bps * 0.7)
        )
    ):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "controlled_late_profit_take"
    elif (
        setup_variant == "resolved_pullback_limit"
        and (
            market_range_15s >= cfg.near_end_max_market_range_15s
            or market_range_30s >= cfg.paper_profit_take_on_market_range_30s
            or adverse_spot_bps >= cfg.controlled_late_max_adverse_spot_15s_bps
        )
    ):
        trade.mode = "idle"
        trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
        trade.exit_reason = "resolved_pullback_exit"
    elif resolved_pullback_late_hold:
        trade.hold_to_resolution = True

    if hold_winner_to_resolution and secs_to_end is not None and secs_to_end <= resolution_settle_secs:
        trade.mode = "idle"
        trade.exit_price = 1.0 if side_winning else 0.0
        trade.exit_reason = "resolution_win" if side_winning else "resolution_loss"
    elif (
        not hold_winner_to_resolution
        and bid_now >= _safe_float(trade.target_price)
        and (setup_variant != "resolved_pullback_limit" or resolved_pullback_early_target_ok)
    ):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "target"
    elif bid_now <= _safe_float(trade.stop_price):
        trade.mode = "idle"
        trade.exit_price = bid_now
        trade.exit_reason = "stop"
    elif (
        pnl_ticks_now >= cfg.paper_profit_take_min_ticks
        and not resolved_pullback_late_hold
        and (
            (not hold_winner_to_resolution and secs_to_end is not None and secs_to_end <= cfg.paper_profit_take_late_secs)
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
        and not hold_winner_to_resolution
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
    elif not hold_winner_to_resolution and secs_to_end is not None and secs_to_end <= cfg.min_secs_to_end:
        trade.mode = "idle"
        trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
        trade.exit_reason = "deadline"
    elif not hold_winner_to_resolution and not trade.hold_to_resolution and now - trade.created_at >= cfg.max_hold_secs:
        trade.mode = "idle"
        trade.exit_price = bid_now if bid_now > 0 else trade.entry_price
        trade.exit_reason = "timeout"

    if trade.mode == "idle" and trade.exit_price is not None and trade.entry_price is not None:
        trade.pnl_ticks = round((trade.exit_price - trade.entry_price) / tick_size, 4)
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
        "total_estimated_rebate": total_estimated_rebate,
        "total_filled_qty": total_qty,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trade the current almost-resolved setup in isolation")
    parser.add_argument("--seconds", type=int, default=300, help="Run duration. Use 0 to run indefinitely.")
    parser.add_argument("--poll-secs", type=float, default=2.0, help="Polling interval")
    parser.add_argument("--log-file", type=str, default=None, help="Optional JSONL log path")
    parser.add_argument("--passive-capture-only", action="store_true", help="Only simulate passive extreme-liquidity-capture entries")
    parser.add_argument("--order-qty", type=float, default=50.0, help="Passive paper order size in contracts")
    parser.add_argument("--maker-rebate-bps", type=float, default=0.0, help="Estimated maker rebate in bps of filled notional")
    parser.add_argument(
        "--hybrid-passive-to-aggressive",
        action="store_true",
        help="Post passive one-tick-below entries first, then replace with a marketable limit if still unfilled.",
    )
    parser.add_argument(
        "--hybrid-aggressive-after-secs",
        type=float,
        default=1.5,
        help="Seconds to wait before replacing an unfilled passive order with an aggressive limit.",
    )
    parser.add_argument(
        "--hybrid-aggressive-max-price",
        type=float,
        default=0.99,
        help="Maximum buy limit price allowed for the aggressive replacement order.",
    )
    parser.add_argument(
        "--hold-winner-to-resolution",
        action="store_true",
        help="Do not exit by target/deadline/timeout; keep stop/profit-protection and settle near resolution.",
    )
    parser.add_argument(
        "--resolution-settle-secs",
        type=int,
        default=1,
        help="When holding to resolution, mark the trade as 1.00/0.00 this many seconds before the end.",
    )
    args = parser.parse_args()

    signal_cfg = CurrentAlmostResolvedConfigV1()
    scalp_cfg = CurrentScalpConfigV1()
    current_scalp = CurrentScalpResearchV1(cfg=scalp_cfg)
    overlay_engine = ManualOverlayEngineV1(scalp_cfg=scalp_cfg, signal_cfg=signal_cfg)
    trade = PaperTrade()
    order = PaperOrder(total_qty=float(args.order_qty))
    log_path = Path(args.log_file) if args.log_file else _build_default_log_path()
    completed: list[dict] = []
    blocked_reasons = Counter()
    allowed_variants = Counter()
    entered_variants = Counter()
    exit_reasons = Counter()
    order_events = Counter()
    last_leader_price_by_key: dict[str, float] = {}

    print("[CURRENT_ALMOST_RESOLVED_CONFIG]")
    pprint(signal_cfg.as_dict())
    print("[CURRENT_SCALP_CONTEXT_CONFIG]")
    pprint(scalp_cfg.as_dict())
    print("[LOG_FILE]")
    print(log_path)

    started_at = time.time()
    current_open_reference: dict[str, object | None] = {"slug": None, "price": None, "event_start_time": None}

    while args.seconds <= 0 or time.time() - started_at < args.seconds:
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
        signal = dict(signal)
        signal["current_slug"] = current_item.get("slug")
        signal["signed_distance_from_open_bps"] = current_scalp_signal.get("distance_from_open_bps")
        signal["guardian_hold_winner_to_resolution"] = bool(args.hold_winner_to_resolution)
        if args.passive_capture_only and signal.get("setup_variant") != "passive_extreme_liquidity_capture":
            signal["allow"] = False
            signal["reason"] = f"passive_capture_only_skipped_{signal.get('setup_variant') or 'none'}"

        if not signal.get("allow"):
            blocked_reasons[str(signal.get("reason") or "unknown")] += 1
        else:
            allowed_variants[str(signal.get("setup_variant") or "standard")] += 1

        print("\n===== CURRENT ALMOST RESOLVED PAPER V1 =====")
        print(
            f"current_secs={current_secs} exec_reason={current_exec_reason} "
            f"allow={signal.get('allow')} side={signal.get('side')} trade_mode={trade.mode} "
            f"order_active={order.active}"
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
                "order": asdict(order),
            },
        )

        suggested_action, suggested_detail, risk_plan, exit_alert = overlay_engine._suggested_action(
            current_secs=current_secs,
            setup_side=str(signal.get("side") or ""),
            trend_side=str(current_scalp_signal.get("side") or "NEUTRAL"),
            almost_signal=signal,
            scalp_signal=current_scalp_signal,
            reversal_risk=overlay_engine._infer_reversal_risk(signal, current_scalp_signal),
            score=overlay_engine._manual_score(
                signal,
                current_scalp_signal,
                overlay_engine._infer_reversal_risk(signal, current_scalp_signal),
                overlay_engine._infer_trend(current_scalp_signal)[1],
            ),
        )
        print(f"[SUGGESTED_ACTION] {suggested_action} | {suggested_detail} | {risk_plan} | {exit_alert}")

        passive_signal = (
            signal.get("allow")
            and signal.get("setup_variant") == "passive_extreme_liquidity_capture"
            and signal.get("execution_style") == "post_only"
        )
        signal_side = str(signal.get("side") or "")
        signal_slug = str(current_item.get("slug") or "")
        leader_price = _safe_float(signal.get("leader_price"), 0.0)
        leader_key = f"{signal_slug}:{signal_side}"
        previous_leader_price = last_leader_price_by_key.get(leader_key)
        hybrid_limit_signal = bool(args.hybrid_passive_to_aggressive and signal.get("allow") and signal_side in ("UP", "DOWN"))
        staged_limit_signal = bool(passive_signal or hybrid_limit_signal)

        if order.active:
            signal_order_reference_price = _safe_float(
                signal.get("limit_price"),
                _safe_float(signal.get("entry_price"), 0.0),
            )
            order_still_valid = (
                staged_limit_signal
                and order.slug == signal_slug
                and order.side == signal_side
                and signal_order_reference_price >= _safe_float(order.limit_price, 0.0)
            )
            too_late = current_secs is not None and current_secs <= signal_cfg.min_secs_to_end
            if not order_still_valid or too_late:
                order_events["cancel"] += 1
                _append_jsonl(
                    log_path,
                    {
                        "type": "cancel",
                        "ts": now,
                        "reason": "context_deteriorated_or_deadline",
                        "current_secs": current_secs,
                        "signal": signal,
                        "order": asdict(order),
                    },
                )
                print("[PAPER_CANCEL]")
                pprint(asdict(order))
                order = PaperOrder(total_qty=float(args.order_qty))
            elif order.side:
                tick_size = _tick_size_from_snap(current_snap, order.side)
                remaining_qty = max(0.0, _safe_float(order.total_qty, args.order_qty) - _safe_float(order.filled_qty, 0.0))
                available_qty = _available_ask_qty_at_or_below(current_snap, order.side, _safe_float(order.limit_price, 0.0))
                fill_qty = round(min(remaining_qty, available_qty), 6)
                if (
                    fill_qty <= 0
                    and args.hybrid_passive_to_aggressive
                    and order.order_style == "passive_limit"
                    and staged_limit_signal
                    and trade.mode == "idle"
                    and now - _safe_float(order.created_at, now) >= max(0.0, float(args.hybrid_aggressive_after_secs))
                ):
                    raw_ask = _ask_for_side(current_snap, order.side)
                    aggressive_price = round(raw_ask, 6) if raw_ask > 0 else 0.0
                    if 0 < aggressive_price <= float(args.hybrid_aggressive_max_price):
                        order_events["replace_aggressive_limit"] += 1
                        _append_jsonl(
                            log_path,
                            {
                                "type": "replace_aggressive_limit",
                                "ts": now,
                                "reason": "passive_unfilled_cross_current_ask",
                                "passive_order": asdict(order),
                                "raw_ask": raw_ask,
                                "aggressive_limit_price": aggressive_price,
                                "signal": signal,
                            },
                        )
                        print("[PAPER_REPLACE_AGGRESSIVE_LIMIT]")
                        pprint({"from": asdict(order), "raw_ask": raw_ask, "limit_price": aggressive_price})
                        order.limit_price = aggressive_price
                        order.order_style = "aggressive_limit"
                        order.updated_at = now
                        available_qty = _available_ask_qty_at_or_below(current_snap, order.side, aggressive_price)
                        fill_qty = round(min(remaining_qty, available_qty), 6)
                    else:
                        order_events["aggressive_limit_skip"] += 1
                        _append_jsonl(
                            log_path,
                            {
                                "type": "aggressive_limit_skip",
                                "ts": now,
                                "reason": "ask_missing_or_above_max",
                                "passive_order": asdict(order),
                                "raw_ask": raw_ask,
                                "max_price": float(args.hybrid_aggressive_max_price),
                                "signal": signal,
                            },
                        )
                if fill_qty > 0 and order.limit_price is not None:
                    was_idle = trade.mode == "idle"
                    order.filled_qty = round(_safe_float(order.filled_qty, 0.0) + fill_qty, 6)
                    order.updated_at = now
                    filled_signal = dict(signal)
                    filled_signal["entry_price"] = _safe_float(order.limit_price, 0.0)
                    filled_signal["limit_price"] = _safe_float(order.limit_price, 0.0)
                    filled_signal["execution_style"] = order.order_style
                    filled_signal["hybrid_entry_style"] = order.order_style
                    trade = _apply_passive_fill(
                        trade,
                        signal=filled_signal,
                        fill_price=_safe_float(order.limit_price, 0.0),
                        fill_qty=fill_qty,
                        tick_size=tick_size,
                        now=now,
                        cfg=signal_cfg,
                        maker_rebate_bps=float(args.maker_rebate_bps),
                    )
                    if was_idle:
                        entered_variants[str(trade.setup_variant or "standard")] += 1
                    order_events["fill"] += 1
                    _append_jsonl(
                        log_path,
                        {
                            "type": "fill",
                            "ts": now,
                            "fill_qty": fill_qty,
                            "available_qty": available_qty,
                            "signal": filled_signal,
                            "order": asdict(order),
                            "trade": asdict(trade),
                        },
                    )
                    print("[PAPER_FILL]")
                    pprint({"fill_qty": fill_qty, "order": asdict(order), "trade": asdict(trade)})
                    if order.filled_qty >= order.total_qty:
                        order = PaperOrder(total_qty=float(args.order_qty))

        if staged_limit_signal and trade.mode == "idle" and not order.active and signal_side in ("UP", "DOWN"):
            tick_up_confirmed = not passive_signal or previous_leader_price is None or leader_price > previous_leader_price
            tick_size = _tick_size_from_snap(current_snap, signal_side)
            default_limit_price = round(max(0.01, _safe_float(signal.get("entry_price"), 0.0) - tick_size), 6)
            limit_price = _safe_float(signal.get("limit_price"), default_limit_price)
            raw_ask = _ask_for_side(current_snap, signal_side)
            if tick_up_confirmed and limit_price > 0 and (raw_ask <= 0 or limit_price < raw_ask):
                order_style = "passive_limit"
                order = PaperOrder(
                    active=True,
                    slug=signal_slug,
                    side=signal_side,
                    limit_price=limit_price,
                    order_style=order_style,
                    total_qty=float(args.order_qty),
                    filled_qty=0.0,
                    created_at=now,
                    updated_at=now,
                )
                order_events["place"] += 1
                _append_jsonl(
                    log_path,
                    {
                        "type": "place",
                        "ts": now,
                        "signal": signal,
                        "suggested_action": suggested_action,
                        "suggested_detail": suggested_detail,
                        "tick_up_confirmed": tick_up_confirmed,
                        "previous_leader_price": previous_leader_price,
                        "raw_ask": raw_ask,
                        "hybrid_limit_signal": hybrid_limit_signal,
                        "order": asdict(order),
                    },
                )
                print("[PAPER_PLACE_PASSIVE]")
                pprint(asdict(order))
            else:
                order_events["watch"] += 1
                _append_jsonl(
                    log_path,
                    {
                        "type": "watch",
                        "ts": now,
                        "reason": "waiting_tick_up_or_post_only",
                        "tick_up_confirmed": tick_up_confirmed,
                        "previous_leader_price": previous_leader_price,
                        "raw_ask": raw_ask,
                        "signal": signal,
                    },
                )
        elif (
            not args.passive_capture_only
            and not args.hybrid_passive_to_aggressive
            and not passive_signal
            and trade.mode == "idle"
            and signal.get("allow")
            and (suggested_action.startswith("COMPRAR ") or suggested_action.startswith("LIMITE "))
        ):
            tick_size = _tick_size_from_snap(current_snap, signal.get("side") or "UP")
            trade = _paper_enter(signal, tick_size, now, signal_cfg)
            entered_variants[str(trade.setup_variant or "standard")] += 1
            _append_jsonl(
                log_path,
                {
                    "type": "enter",
                    "ts": now,
                    "signal": signal,
                    "suggested_action": suggested_action,
                    "suggested_detail": suggested_detail,
                    "trade": asdict(trade),
                },
            )
            print("[PAPER_ENTER]")
            pprint(asdict(trade))

        if signal_side in ("UP", "DOWN") and leader_price > 0:
            last_leader_price_by_key[leader_key] = leader_price

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
                hold_winner_to_resolution=bool(args.hold_winner_to_resolution),
                resolution_settle_secs=max(0, int(args.resolution_settle_secs)),
            )
            print("[PAPER_MANAGE]")
            pprint(asdict(trade))
            if trade.mode == "idle" and trade.exit_reason is not None:
                completed.append(asdict(trade))
                exit_reasons[str(completed[-1].get("exit_reason") or "unknown")] += 1
                _append_jsonl(log_path, {"type": "exit", "ts": now, "trade": completed[-1]})
                print("[PAPER_EXIT]")
                pprint(completed[-1])
                trade = PaperTrade()

        time.sleep(max(0.5, float(args.poll_secs)))

    print("\n[SUMMARY]")
    pprint(
        {
            "stats": _trade_stats(completed),
            "allowed_variants": dict(allowed_variants),
            "entered_variants": dict(entered_variants),
            "exit_reasons": dict(exit_reasons),
            "order_events": dict(order_events),
            "blocked_reasons_top10": blocked_reasons.most_common(10),
            "log_file": str(log_path),
            "trades": completed,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
