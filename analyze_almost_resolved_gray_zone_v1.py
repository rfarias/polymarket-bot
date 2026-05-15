from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1
from market.manual_overlay_v1 import ManualOverlayEngineV1


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _side_bid(signal: dict[str, Any], side: str) -> float:
    return _safe_float(signal.get("up_sell" if side == "UP" else "down_sell"), 0.0)


def _side_ask(signal: dict[str, Any], side: str) -> float:
    return _safe_float(signal.get("up_buy" if side == "UP" else "down_buy"), 0.0)


def _counter_ask(signal: dict[str, Any], side: str) -> float:
    return _safe_float(signal.get("down_buy" if side == "UP" else "up_buy"), 0.0)


def _signed_distance(row: dict[str, Any], signal: dict[str, Any]) -> float:
    ctx = row.get("current_scalp_context") or {}
    return _safe_float(signal.get("signed_distance_from_open_bps"), _safe_float(ctx.get("distance_from_open_bps"), 0.0))


def _winner_for_side(row: dict[str, Any], signal: dict[str, Any], side: str) -> bool:
    signed = _signed_distance(row, signal)
    return (side == "UP" and signed > 0) or (side == "DOWN" and signed < 0)


def _reversal_risk(engine: ManualOverlayEngineV1, signal: dict[str, Any], ctx: dict[str, Any]) -> str:
    return engine._infer_reversal_risk(signal, ctx)


def _passive_score(signal: dict[str, Any], side: str) -> int:
    key = "passive_capture_up_score" if side == "UP" else "passive_capture_down_score"
    return int(_safe_float(signal.get(key), 0.0))


def _gray_candidate(
    *,
    row: dict[str, Any],
    cfg: CurrentAlmostResolvedConfigV1,
    engine: ManualOverlayEngineV1,
    min_score: int,
    max_score: int,
    min_distance_usd: float,
    min_distance_bps: float,
    min_leader_price: float,
    max_counter_price: float,
    max_secs_to_end: int,
    allow_high_reversal: bool,
) -> tuple[bool, str, str]:
    signal = row.get("signal") or {}
    ctx = row.get("current_scalp_context") or {}
    secs = row.get("current_secs")
    side = str(signal.get("side") or "")
    if side not in ("UP", "DOWN"):
        side = "UP" if _safe_float(signal.get("up_buy"), 0.0) >= _safe_float(signal.get("down_buy"), 0.0) else "DOWN"

    if secs is None or int(secs) < cfg.extreme_99_min_secs_to_end or int(secs) > max_secs_to_end:
        return False, side, "outside_time_window"

    leader = _side_ask(signal, side)
    counter = _counter_ask(signal, side)
    distance_usd = _safe_float(signal.get("distance_to_price_to_beat_usd"), 0.0)
    distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), 0.0))
    recent_vol = _safe_float(signal.get("resolved_pullback_recent_vol_floor_usd"), 0.0)
    buffer_usd = _safe_float(signal.get("up_price_to_beat_buffer_usd" if side == "UP" else "down_price_to_beat_buffer_usd"), 0.0)
    buffer_bps = _safe_float(signal.get("up_price_to_beat_buffer_bps" if side == "UP" else "down_price_to_beat_buffer_bps"), 0.0)
    adverse_5s = max(0.0, -_safe_float(ctx.get("spot_delta_5s_bps"), 0.0) if side == "UP" else _safe_float(ctx.get("spot_delta_5s_bps"), 0.0))
    adverse_15s = max(0.0, -_safe_float(ctx.get("spot_delta_15s_bps"), 0.0) if side == "UP" else _safe_float(ctx.get("spot_delta_15s_bps"), 0.0))
    adverse_30s = max(0.0, -_safe_float(ctx.get("spot_delta_30s_bps"), 0.0) if side == "UP" else _safe_float(ctx.get("spot_delta_30s_bps"), 0.0))
    market_range_30s = _safe_float(signal.get("market_range_30s"), 0.0)
    signed = _signed_distance(row, signal)
    direction_ok = (side == "UP" and signed > 0) or (side == "DOWN" and signed < 0)
    score = _passive_score(signal, side)
    risk = _reversal_risk(engine, signal, ctx)

    checks = [
        (leader >= min_leader_price, "leader_price"),
        (counter <= max_counter_price, "counter_price"),
        (distance_usd >= min_distance_usd, "distance_usd"),
        (distance_bps >= min_distance_bps, "distance_bps"),
        (distance_usd >= recent_vol * cfg.passive_capture_min_distance_vs_recent_vol_mult, "distance_vs_vol"),
        (buffer_usd >= cfg.near_end_min_price_to_beat_buffer_usd, "buffer_usd"),
        (buffer_bps >= cfg.near_end_min_price_to_beat_buffer_bps, "buffer_bps"),
        (adverse_5s <= cfg.passive_capture_max_adverse_spot_5s_bps, "adverse_5s"),
        (adverse_15s <= cfg.passive_capture_max_adverse_spot_15s_bps, "adverse_15s"),
        (adverse_30s <= cfg.passive_capture_max_adverse_spot_30s_bps, "adverse_30s"),
        (market_range_30s <= cfg.passive_capture_max_market_range_30s, "market_range_30s"),
        (direction_ok, "direction"),
        (score >= min_score and score <= max_score, "gray_score"),
        (allow_high_reversal or risk != "high", "reversal_risk"),
    ]
    failed = [name for ok, name in checks if not ok]
    return not failed, side, failed[0] if failed else "gray_ok"


def _green_candidate(row: dict[str, Any], engine: ManualOverlayEngineV1) -> tuple[bool, str]:
    signal = row.get("signal") or {}
    ctx = row.get("current_scalp_context") or {}
    side = str(signal.get("side") or "")
    if side not in ("UP", "DOWN"):
        return False, side
    risk = _reversal_risk(engine, signal, ctx)
    score = engine._manual_score(signal, ctx, risk, engine._infer_trend(ctx)[1])
    passive_score = _passive_score(signal, side)
    return bool(signal.get("allow")) and risk != "high" and score >= 55 and passive_score >= 85, side


@dataclass
class SimTrade:
    mode: str
    slug: str
    side: str
    entry_ts: float
    entry_secs: int
    entry_price: float
    stop_price: float
    target_price: float
    source: str
    promoted_to_hold: bool = False
    exit_ts: float | None = None
    exit_secs: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_ticks: float | None = None


def _close_trade(trade: SimTrade, row: dict[str, Any], price: float, reason: str, tick: float) -> None:
    trade.mode = "closed"
    trade.exit_ts = _safe_float(row.get("ts"), trade.entry_ts)
    trade.exit_secs = int(_safe_float(row.get("current_secs"), 0.0))
    trade.exit_price = round(max(0.0, min(1.0, price)), 6)
    trade.exit_reason = reason
    trade.pnl_ticks = round((trade.exit_price - trade.entry_price) / tick, 4)


def _simulate(
    rows: list[dict[str, Any]],
    *,
    cfg: CurrentAlmostResolvedConfigV1,
    engine: ManualOverlayEngineV1,
    gray_min_score: int,
    gray_max_score: int,
    gray_min_distance_usd: float,
    gray_min_distance_bps: float,
    gray_min_leader_price: float,
    gray_max_counter_price: float,
    gray_max_secs_to_end: int,
    target_ticks: int,
    stop_ticks: int,
    allow_high_reversal: bool,
    include_trades: bool = False,
) -> dict[str, Any]:
    tick = 0.01
    open_trade: SimTrade | None = None
    completed: list[SimTrade] = []
    blocked = Counter()
    entries = Counter()
    exits = Counter()
    previous_snapshot: dict[str, Any] | None = None

    for row in rows:
        if row.get("type") != "snapshot":
            continue
        signal = row.get("signal") or {}
        slug = str(row.get("current_slug") or signal.get("current_slug") or "")
        secs = int(_safe_float(row.get("current_secs"), 0.0))

        if open_trade is not None and open_trade.mode == "open" and slug != open_trade.slug:
            settle_row = previous_snapshot or row
            settle_signal = settle_row.get("signal") or {}
            _close_trade(
                open_trade,
                settle_row,
                1.0 if _winner_for_side(settle_row, settle_signal, open_trade.side) else 0.0,
                "resolution_slug_roll",
                tick,
            )
            exits[str(open_trade.exit_reason)] += 1
            completed.append(open_trade)
            open_trade = None

        green, green_side = _green_candidate(row, engine)
        gray, gray_side, gray_reason = _gray_candidate(
            row=row,
            cfg=cfg,
            engine=engine,
            min_score=gray_min_score,
            max_score=gray_max_score,
            min_distance_usd=gray_min_distance_usd,
            min_distance_bps=gray_min_distance_bps,
            min_leader_price=gray_min_leader_price,
            max_counter_price=gray_max_counter_price,
            max_secs_to_end=gray_max_secs_to_end,
            allow_high_reversal=allow_high_reversal,
        )

        if open_trade is not None and open_trade.mode == "open":
            bid = _side_bid(signal, open_trade.side)
            if not open_trade.promoted_to_hold and green and green_side == open_trade.side:
                open_trade.promoted_to_hold = True
            if bid > 0 and bid <= open_trade.stop_price:
                _close_trade(open_trade, row, bid, "gray_stop" if not open_trade.promoted_to_hold else "hold_stop", tick)
            elif not open_trade.promoted_to_hold and bid > 0 and bid >= open_trade.target_price:
                _close_trade(open_trade, row, bid, "gray_target", tick)
            elif open_trade.promoted_to_hold and secs <= 1:
                _close_trade(open_trade, row, 1.0 if _winner_for_side(row, signal, open_trade.side) else 0.0, "resolution", tick)
            elif not open_trade.promoted_to_hold and secs <= cfg.min_secs_to_end:
                _close_trade(open_trade, row, bid if bid > 0 else open_trade.entry_price, "gray_deadline", tick)

            if open_trade.mode == "closed":
                exits[str(open_trade.exit_reason)] += 1
                completed.append(open_trade)
                open_trade = None

        if open_trade is None:
            side = green_side if green else gray_side
            source = "green_hold" if green else "gray_target_stop" if gray else ""
            if source and side in ("UP", "DOWN"):
                ask = _side_ask(signal, side)
                if ask > 0:
                    target_price = 1.0 if source == "green_hold" else min(0.99, ask + target_ticks * tick)
                    stop_price = max(0.01, ask - (cfg.stop_ticks if source == "green_hold" else stop_ticks) * tick)
                    open_trade = SimTrade(
                        mode="open",
                        slug=slug,
                        side=side,
                        entry_ts=_safe_float(row.get("ts")),
                        entry_secs=secs,
                        entry_price=round(ask, 6),
                        stop_price=round(stop_price, 6),
                        target_price=round(target_price, 6),
                        source=source,
                        promoted_to_hold=source == "green_hold",
                    )
                    entries[source] += 1
            elif gray_reason:
                blocked[gray_reason] += 1

        previous_snapshot = row

    pnls = [_safe_float(t.pnl_ticks) for t in completed]
    return {
        "gray_min_score": gray_min_score,
        "gray_max_score": gray_max_score,
        "gray_min_distance_usd": gray_min_distance_usd,
        "gray_min_distance_bps": gray_min_distance_bps,
        "gray_min_leader_price": gray_min_leader_price,
        "gray_max_counter_price": gray_max_counter_price,
        "gray_max_secs_to_end": gray_max_secs_to_end,
        "target_ticks": target_ticks,
        "stop_ticks": stop_ticks,
        "allow_high_reversal": allow_high_reversal,
        "entries": dict(entries),
        "completed": len(completed),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p < 0),
        "flat": sum(1 for p in pnls if p == 0),
        "total_pnl_ticks": round(sum(pnls), 4),
        "avg_pnl_ticks": round(mean(pnls), 4) if pnls else None,
        "exit_reasons": dict(exits),
        "blocked_top": blocked.most_common(8),
        "trades": [asdict(t) for t in completed] if include_trades else [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay almost-resolved logs with a gray-zone target/stop mode")
    parser.add_argument("log_file", type=str)
    parser.add_argument("--gray-min-score", type=int, default=70)
    parser.add_argument("--gray-max-score", type=int, default=84)
    parser.add_argument("--gray-min-distance-usd", type=float, default=50.0)
    parser.add_argument("--gray-min-distance-bps", type=float, default=5.0)
    parser.add_argument("--gray-min-leader-price", type=float, default=0.98)
    parser.add_argument("--gray-max-counter-price", type=float, default=0.03)
    parser.add_argument("--gray-max-secs-to-end", type=int, default=60)
    parser.add_argument("--target-ticks", type=int, default=1)
    parser.add_argument("--stop-ticks", type=int, default=2)
    parser.add_argument("--allow-high-reversal", action="store_true")
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--output", type=str, default="")
    args = parser.parse_args()

    rows = _load_rows(Path(args.log_file))
    cfg = CurrentAlmostResolvedConfigV1()
    engine = ManualOverlayEngineV1(signal_cfg=cfg)

    if args.matrix:
        results = []
        for max_secs in (60, 75, 90):
          for min_score in (35, 45, 55, 65):
            for min_distance in (40.0, 50.0, 60.0, 70.0):
                for target_ticks in (1, 2):
                    for stop_ticks in (1, 2, 3):
                        results.append(
                            _simulate(
                                rows,
                                cfg=cfg,
                                engine=engine,
                                gray_min_score=min_score,
                                gray_max_score=84,
                                gray_min_distance_usd=min_distance,
                                gray_min_distance_bps=float(args.gray_min_distance_bps),
                                gray_min_leader_price=float(args.gray_min_leader_price),
                                gray_max_counter_price=float(args.gray_max_counter_price),
                                gray_max_secs_to_end=max_secs,
                                target_ticks=target_ticks,
                                stop_ticks=stop_ticks,
                                allow_high_reversal=bool(args.allow_high_reversal),
                                include_trades=False,
                            )
                        )
        payload: dict[str, Any] = {"log_file": args.log_file, "results": results}
    else:
        payload = {
            "log_file": args.log_file,
            "result": _simulate(
                rows,
                cfg=cfg,
                engine=engine,
                gray_min_score=int(args.gray_min_score),
                gray_max_score=int(args.gray_max_score),
                gray_min_distance_usd=float(args.gray_min_distance_usd),
                gray_min_distance_bps=float(args.gray_min_distance_bps),
                gray_min_leader_price=float(args.gray_min_leader_price),
                gray_max_counter_price=float(args.gray_max_counter_price),
                gray_max_secs_to_end=int(args.gray_max_secs_to_end),
                target_ticks=int(args.target_ticks),
                stop_ticks=int(args.stop_ticks),
                allow_high_reversal=bool(args.allow_high_reversal),
                include_trades=True,
            ),
        }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
