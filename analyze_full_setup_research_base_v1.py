from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, _passive_capture_score


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _iter_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") != "snapshot":
                continue
            if row.get("runner") not in ("current_almost_resolved", "current_almost_resolved_gray_zone"):
                continue
            if not row.get("slug"):
                continue
            rows.append(row)
    rows.sort(key=lambda r: (str(r.get("source_file") or ""), str(r.get("slug") or ""), _safe_float(r.get("ts"))))
    return rows


def _side_price(row: dict[str, Any], side: str) -> float:
    row_side = str(row.get("side") or "")
    leader = _safe_float(row.get("leader_price"), 0.0)
    counter = _safe_float(row.get("counter_price"), 0.0)
    if side == row_side:
        return leader
    if row_side in ("UP", "DOWN"):
        return counter
    return leader if side == "UP" else counter


def _side_bid(row: dict[str, Any], side: str, tick: float) -> float:
    # The normalized research base does not preserve full book levels. Use one tick
    # below the observed executable side price as a conservative bid proxy.
    price = _side_price(row, side)
    return round(max(0.0, price - tick), 6) if price > 0 else 0.0


def _winner(row: dict[str, Any], side: str) -> bool:
    signed = _safe_float(row.get("distance_from_open_bps"), 0.0)
    return (side == "UP" and signed > 0) or (side == "DOWN" and signed < 0)


def _passive_score(row: dict[str, Any], cfg: CurrentAlmostResolvedConfigV1, side: str, safe_distance_ok: bool) -> int:
    secs = _safe_int(row.get("secs_to_end"), 0)
    leader = _side_price(row, side)
    counter = _side_price(row, "DOWN" if side == "UP" else "UP")
    spot_5s = _safe_float(row.get("spot_delta_5s_bps"), 0.0)
    adverse = max(0.0, -spot_5s if side == "UP" else spot_5s)
    return _passive_capture_score(
        leader_price=leader,
        counter_price=counter,
        secs_to_end=secs,
        distance_usd=_safe_float(row.get("distance_to_price_to_beat_usd"), 0.0),
        distance_bps=abs(_safe_float(row.get("distance_to_price_to_beat_bps"), _safe_float(row.get("distance_from_open_bps"), 0.0))),
        safe_distance_ok=safe_distance_ok,
        adverse_spot_bps=adverse,
        market_range_30s=_safe_float(row.get("market_range_30s"), 999.0),
        depth=100.0,
        cfg=cfg,
    )


def _base_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    side = str(row.get("side") or "")
    if row.get("allow") is not True or side not in ("UP", "DOWN"):
        return None
    entry = _safe_float(row.get("entry_price"), 0.0)
    if entry <= 0:
        entry = _side_price(row, side)
    if entry <= 0:
        return None
    variant = str(row.get("setup_variant") or "standard")
    return {
        "source": "base_signal",
        "variant": variant,
        "side": side,
        "entry_price": round(entry, 6),
        "target_price": round(min(0.99, _safe_float(row.get("exit_price"), 0.99)), 6),
        "stop_ticks": 3,
        "hold_to_resolution": True,
    }


def _split_extreme_ok(row: dict[str, Any], candidate: dict[str, Any], args: argparse.Namespace) -> bool:
    if not bool(args.split_extreme_entry):
        return False
    if str(candidate.get("variant") or "") != "passive_extreme_liquidity_capture":
        return False
    secs = _safe_int(row.get("secs_to_end"), 0)
    if secs > 45:
        return False
    side = str(candidate.get("side") or "")
    leader = _side_price(row, side)
    counter = _side_price(row, "DOWN" if side == "UP" else "UP")
    if leader < 0.98 or leader > float(args.hybrid_aggressive_max_price) or counter > 0.03:
        return False
    if _safe_float(row.get("market_range_30s"), 999.0) > 0.045:
        return False
    distance_usd = _safe_float(row.get("distance_to_price_to_beat_usd"), 0.0)
    distance_bps = abs(_safe_float(row.get("distance_to_price_to_beat_bps"), 0.0))
    return distance_usd >= 50.0 and distance_bps >= 5.0


def _gray_candidate(row: dict[str, Any], cfg: CurrentAlmostResolvedConfigV1, args: argparse.Namespace) -> tuple[dict[str, Any] | None, str]:
    secs = _safe_int(row.get("secs_to_end"), -1)
    side = str(row.get("side") or "")
    if side not in ("UP", "DOWN"):
        return None, "missing_side"
    if secs < cfg.extreme_99_min_secs_to_end or secs > int(args.gray_max_secs_to_end):
        return None, "outside_time_window"
    leader = _side_price(row, side)
    counter = _side_price(row, "DOWN" if side == "UP" else "UP")
    distance_usd = _safe_float(row.get("distance_to_price_to_beat_usd"), -1.0)
    distance_bps = abs(_safe_float(row.get("distance_to_price_to_beat_bps"), _safe_float(row.get("distance_from_open_bps"), 0.0)))
    buffer_usd = _safe_float(row.get("buffer_usd"), 0.0)
    buffer_bps = _safe_float(row.get("buffer_bps"), 0.0)
    spot_5s = _safe_float(row.get("spot_delta_5s_bps"), 0.0)
    spot_15s = _safe_float(row.get("spot_delta_15s_bps"), 0.0)
    spot_30s = _safe_float(row.get("spot_delta_30s_bps"), 0.0)
    adverse_5s = max(0.0, -spot_5s if side == "UP" else spot_5s)
    adverse_15s = max(0.0, -spot_15s if side == "UP" else spot_15s)
    adverse_30s = max(0.0, -spot_30s if side == "UP" else spot_30s)
    market_range_30s = _safe_float(row.get("market_range_30s"), 999.0)
    direction_ok = _winner(row, side)
    safe_distance_ok = distance_usd >= float(args.gray_min_distance_usd) and distance_bps >= float(args.gray_min_distance_bps)
    score = _passive_score(row, cfg, side, safe_distance_ok)

    checks = [
        (leader >= float(args.gray_min_leader_price), "leader_price"),
        (counter <= float(args.gray_max_counter_price), "counter_price"),
        (distance_usd >= float(args.gray_min_distance_usd), "distance_usd"),
        (distance_bps >= float(args.gray_min_distance_bps), "distance_bps"),
        (buffer_usd >= cfg.near_end_min_price_to_beat_buffer_usd, "buffer_usd"),
        (buffer_bps >= cfg.near_end_min_price_to_beat_buffer_bps, "buffer_bps"),
        (adverse_5s <= cfg.passive_capture_max_adverse_spot_5s_bps, "adverse_5s"),
        (adverse_15s <= cfg.passive_capture_max_adverse_spot_15s_bps, "adverse_15s"),
        (adverse_30s <= cfg.passive_capture_max_adverse_spot_30s_bps, "adverse_30s"),
        (market_range_30s <= cfg.passive_capture_max_market_range_30s, "market_range_30s"),
        (direction_ok, "direction"),
        (int(args.gray_min_score) <= score <= int(args.gray_max_score), "gray_score"),
    ]
    failed = [name for ok, name in checks if not ok]
    if failed:
        return None, failed[0]
    entry = round(max(0.01, leader - 0.01), 6) if args.hybrid else round(leader, 6)
    return {
        "source": "gray_zone",
        "variant": "gray_zone_target_stop",
        "side": side,
        "entry_price": entry,
        "target_price": round(min(0.99, entry + int(args.gray_target_ticks) * 0.01), 6),
        "stop_ticks": int(args.gray_stop_ticks),
        "hold_to_resolution": False,
        "score": score,
    }, "gray_ok"


@dataclass
class Trade:
    source_file: str
    slug: str
    source: str
    variant: str
    side: str
    entry_ts: float
    entry_secs: int
    signal_entry_price: float
    fill_price: float
    target_price: float
    stop_price: float
    fill_style: str
    exit_ts: float | None = None
    exit_secs: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_ticks: float | None = None


def _close(trade: Trade, row: dict[str, Any], price: float, reason: str, tick: float) -> None:
    trade.exit_ts = _safe_float(row.get("ts"), trade.entry_ts)
    trade.exit_secs = _safe_int(row.get("secs_to_end"), 0)
    trade.exit_price = round(max(0.0, min(1.0, price)), 6)
    trade.exit_reason = reason
    trade.pnl_ticks = round((trade.exit_price - trade.fill_price) / tick, 4)


def _simulate_group(rows: list[dict[str, Any]], cfg: CurrentAlmostResolvedConfigV1, args: argparse.Namespace) -> tuple[list[Trade], Counter, Counter]:
    tick = 0.01
    rows = sorted(rows, key=lambda r: _safe_float(r.get("ts")))
    trades: list[Trade] = []
    blocked = Counter()
    funnel = Counter()
    open_trade: Trade | None = None
    pending: dict[str, Any] | None = None
    previous: dict[str, Any] | None = None

    for row in rows:
        secs = _safe_int(row.get("secs_to_end"), 0)
        slug = str(row.get("slug") or "")

        if open_trade is not None:
            bid = _side_bid(row, open_trade.side, tick)
            base_same_side = row.get("allow") is True and str(row.get("side") or "") == open_trade.side
            if open_trade.variant == "gray_zone_target_stop" and base_same_side:
                open_trade.source = "gray_promoted_to_hold"
            if bid > 0 and bid <= open_trade.stop_price:
                _close(open_trade, row, bid, "stop", tick)
            elif open_trade.variant == "gray_zone_target_stop" and open_trade.source != "gray_promoted_to_hold" and bid >= open_trade.target_price:
                _close(open_trade, row, bid, "gray_target", tick)
            elif secs <= int(args.resolution_settle_secs):
                _close(open_trade, row, 1.0 if _winner(row, open_trade.side) else 0.0, "resolution", tick)
            if open_trade.exit_reason:
                trades.append(open_trade)
                open_trade = None
                pending = None
                previous = row
                continue

        if pending is not None:
            side = str(pending["side"])
            candidate = pending["candidate"]
            ask = _side_price(row, side)
            available_at_passive = ask > 0 and ask <= _safe_float(pending["limit_price"], 0.0)
            if available_at_passive:
                pending["touches"] += 1
                funnel["passive_touch"] += 1
            else:
                pending["touches"] = 0
            if pending["touches"] >= int(args.passive_fill_touch_polls):
                fill_price = _safe_float(pending["limit_price"], 0.0)
                funnel["passive_fill"] += 1
                open_trade = Trade(
                    source_file=str(row.get("source_file") or ""),
                    slug=slug,
                    source=str(candidate["source"]),
                    variant=str(candidate["variant"]),
                    side=side,
                    entry_ts=_safe_float(row.get("ts")),
                    entry_secs=secs,
                    signal_entry_price=_safe_float(candidate["entry_price"]),
                    fill_price=fill_price,
                    target_price=round(min(0.99, fill_price + int(args.gray_target_ticks) * tick), 6)
                    if candidate["variant"] == "gray_zone_target_stop"
                    else _safe_float(candidate["target_price"], 0.99),
                    stop_price=round(max(0.01, fill_price - int(candidate["stop_ticks"]) * tick), 6),
                    fill_style="passive",
                )
                funnel["trade_opened"] += 1
                pending = None
            elif args.hybrid and _safe_float(row.get("ts")) - _safe_float(pending["created_ts"]) >= float(args.hybrid_aggressive_after_secs):
                ask_now = _side_price(row, side)
                can_chase = (
                    bool(args.aggressive_all_variants)
                    or (
                        str(candidate.get("variant") or "") == "passive_extreme_liquidity_capture"
                        and _safe_int(row.get("secs_to_end"), 0) <= 35
                    )
                )
                if can_chase and 0 < ask_now <= float(args.hybrid_aggressive_max_price):
                    funnel["aggressive_fill"] += 1
                    open_trade = Trade(
                        source_file=str(row.get("source_file") or ""),
                        slug=slug,
                        source=str(candidate["source"]),
                        variant=str(candidate["variant"]),
                        side=side,
                        entry_ts=_safe_float(row.get("ts")),
                        entry_secs=secs,
                        signal_entry_price=_safe_float(candidate["entry_price"]),
                        fill_price=round(ask_now, 6),
                        target_price=round(min(0.99, ask_now + int(args.gray_target_ticks) * tick), 6)
                        if candidate["variant"] == "gray_zone_target_stop"
                        else _safe_float(candidate["target_price"], 0.99),
                        stop_price=round(max(0.01, ask_now - int(candidate["stop_ticks"]) * tick), 6),
                        fill_style="aggressive",
                    )
                    funnel["trade_opened"] += 1
                else:
                    funnel["aggressive_skip"] += 1
                pending = None

        if open_trade is None and pending is None:
            candidate = _base_candidate(row)
            reason = ""
            if candidate is None and args.enable_gray_zone:
                candidate, reason = _gray_candidate(row, cfg, args)
                if reason:
                    blocked[reason] += 1
            if candidate is None:
                blocked[str(row.get("reason") or reason or "blocked")] += 1
            else:
                funnel["signal_allowed"] += 1
                side = str(candidate["side"])
                if _split_extreme_ok(row, candidate, args):
                    ask_now = _side_price(row, side)
                    aggressive_frac = min(1.0, max(0.0, float(args.split_aggressive_frac)))
                    signal_price = _safe_float(candidate["entry_price"])
                    fill_price = round(ask_now, 6)
                    open_trade = Trade(
                        source_file=str(row.get("source_file") or ""),
                        slug=slug,
                        source="split_extreme",
                        variant=str(candidate["variant"]),
                        side=side,
                        entry_ts=_safe_float(row.get("ts")),
                        entry_secs=secs,
                        signal_entry_price=signal_price,
                        fill_price=fill_price,
                        target_price=_safe_float(candidate["target_price"], 0.99),
                        stop_price=round(max(0.01, fill_price - int(candidate["stop_ticks"]) * tick), 6),
                        fill_style=f"split_aggressive_{aggressive_frac:.2f}",
                    )
                    passive_qty = 1.0 - aggressive_frac
                    if passive_qty > 0:
                        pending = {
                            "candidate": candidate,
                            "side": side,
                            "limit_price": round(max(0.01, ask_now - tick), 6),
                            "created_ts": _safe_float(row.get("ts")),
                            "touches": 0,
                        }
                    funnel["split_aggressive_fill"] += 1
                    funnel["split_passive_placed"] += 1 if passive_qty > 0 else 0
                    funnel["trade_opened"] += 1
                elif args.hybrid:
                    limit_price = round(max(0.01, _safe_float(candidate["entry_price"]) - tick), 6)
                    pending = {
                        "candidate": candidate,
                        "side": side,
                        "limit_price": limit_price,
                        "created_ts": _safe_float(row.get("ts")),
                        "touches": 0,
                    }
                    funnel["passive_placed"] += 1
                else:
                    fill_price = _safe_float(candidate["entry_price"])
                    open_trade = Trade(
                        source_file=str(row.get("source_file") or ""),
                        slug=slug,
                        source=str(candidate["source"]),
                        variant=str(candidate["variant"]),
                        side=side,
                        entry_ts=_safe_float(row.get("ts")),
                        entry_secs=secs,
                        signal_entry_price=fill_price,
                        fill_price=fill_price,
                        target_price=_safe_float(candidate["target_price"], 0.99),
                        stop_price=round(max(0.01, fill_price - int(candidate["stop_ticks"]) * tick), 6),
                        fill_style="direct",
                    )
                    funnel["trade_opened"] += 1
        previous = row

    if open_trade is not None and previous is not None:
        _close(open_trade, previous, 1.0 if _winner(previous, open_trade.side) else 0.0, "resolution_slug_roll", tick)
        trades.append(open_trade)

    return trades, blocked, funnel


def _summarize(trades: list[Trade], blocked: Counter, funnel: Counter) -> dict[str, Any]:
    pnls = [_safe_float(t.pnl_ticks) for t in trades if t.pnl_ticks is not None]
    by_variant = Counter(t.variant for t in trades)
    by_source = Counter(t.source for t in trades)
    by_fill = Counter(t.fill_style for t in trades)
    exits = Counter(str(t.exit_reason or "") for t in trades)
    return {
        "completed": len(trades),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p < 0),
        "flat": sum(1 for p in pnls if p == 0),
        "total_pnl_ticks": round(sum(pnls), 4),
        "avg_pnl_ticks": round(mean(pnls), 4) if pnls else None,
        "by_variant": dict(by_variant),
        "by_source": dict(by_source),
        "by_fill_style": dict(by_fill),
        "exit_reasons": dict(exits),
        "execution_funnel": dict(funnel),
        "blocked_top": blocked.most_common(12),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay full almost-resolved setup on normalized research base")
    parser.add_argument("--input", default="logs/research_base/research_events_all_v1.jsonl")
    parser.add_argument("--output", default="logs/research_base/full_setup_replay_v1.json")
    parser.add_argument("--hybrid", action="store_true", default=True)
    parser.add_argument("--no-hybrid", dest="hybrid", action="store_false")
    parser.add_argument(
        "--aggressive-all-variants",
        action="store_true",
        help="Legacy optimistic mode. By default, only passive_extreme can chase aggressively, matching the real runner.",
    )
    parser.add_argument("--hybrid-aggressive-after-secs", type=float, default=2.0)
    parser.add_argument("--hybrid-aggressive-max-price", type=float, default=0.99)
    parser.add_argument("--passive-fill-touch-polls", type=int, default=2)
    parser.add_argument("--resolution-settle-secs", type=int, default=1)
    parser.add_argument("--split-extreme-entry", action="store_true")
    parser.add_argument("--split-aggressive-frac", type=float, default=0.5)
    parser.add_argument("--enable-gray-zone", action="store_true", default=True)
    parser.add_argument("--gray-min-score", type=int, default=35)
    parser.add_argument("--gray-max-score", type=int, default=84)
    parser.add_argument("--gray-min-distance-usd", type=float, default=40.0)
    parser.add_argument("--gray-min-distance-bps", type=float, default=5.0)
    parser.add_argument("--gray-min-leader-price", type=float, default=0.98)
    parser.add_argument("--gray-max-counter-price", type=float, default=0.03)
    parser.add_argument("--gray-max-secs-to-end", type=int, default=75)
    parser.add_argument("--gray-target-ticks", type=int, default=1)
    parser.add_argument("--gray-stop-ticks", type=int, default=2)
    parser.add_argument("--include-trades", action="store_true")
    parser.add_argument(
        "--group-by",
        choices=("slug", "source_file_slug"),
        default="slug",
        help="Use slug to avoid counting the same market repeated across old paper files.",
    )
    args = parser.parse_args()

    cfg = CurrentAlmostResolvedConfigV1()
    rows = _iter_rows(Path(args.input))
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        slug = str(row.get("slug") or "")
        source_file = str(row.get("source_file") or "") if args.group_by == "source_file_slug" else ""
        groups[(source_file, slug)].append(row)

    all_trades: list[Trade] = []
    blocked = Counter()
    funnel = Counter()
    for group in groups.values():
        trades, group_blocked, group_funnel = _simulate_group(group, cfg, args)
        all_trades.extend(trades)
        blocked.update(group_blocked)
        funnel.update(group_funnel)

    payload: dict[str, Any] = {
        "input": args.input,
        "assumptions": {
            "source": "normalized research base, not full raw book",
            "bid_proxy": "observed side price minus 1 tick",
            "passive_fill": f"{args.passive_fill_touch_polls} consecutive snapshots with observed side price <= passive limit",
            "aggressive_fill": f"observed side price <= {args.hybrid_aggressive_max_price}",
            "gray_zone": bool(args.enable_gray_zone),
        },
        "groups": len(groups),
        "group_by": args.group_by,
        "snapshots": len(rows),
        "summary": _summarize(all_trades, blocked, funnel),
    }
    if args.include_trades:
        payload["trades"] = [asdict(t) for t in all_trades]

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
