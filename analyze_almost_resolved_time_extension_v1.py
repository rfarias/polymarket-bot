from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from market.current_almost_resolved_signal_v1 import CurrentAlmostResolvedConfigV1, _passive_capture_score


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _iter_raw_almost_logs(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*.jsonl"):
        text = str(path).replace("\\", "/").lower()
        if "research_base" in text:
            continue
        if "almost" in text and "resolved" in text:
            files.append(path)
    return sorted(files)


def _load_rows(files: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    if not isinstance(row, dict) or row.get("type") != "snapshot":
                        continue
                    row["_source_file"] = str(path)
                    row["_source_line"] = line_no
                    rows.append(row)
        except Exception:
            continue
    rows.sort(key=lambda r: (str(r.get("_source_file")), str(r.get("current_slug") or ""), _safe_float(r.get("ts"))))
    return rows


def _side_bid(signal: dict[str, Any], side: str) -> float:
    return _safe_float(signal.get("up_sell" if side == "UP" else "down_sell"), 0.0)


def _side_ask(signal: dict[str, Any], side: str) -> float:
    return _safe_float(signal.get("up_buy" if side == "UP" else "down_buy"), 0.0)


def _counter_ask(signal: dict[str, Any], side: str) -> float:
    return _safe_float(signal.get("down_buy" if side == "UP" else "up_buy"), 0.0)


def _distance_from_open(row: dict[str, Any], signal: dict[str, Any]) -> float:
    ctx = row.get("current_scalp_context") if isinstance(row.get("current_scalp_context"), dict) else {}
    return _safe_float(signal.get("signed_distance_from_open_bps"), _safe_float(ctx.get("distance_from_open_bps"), 0.0))


def _winner_for_side(row: dict[str, Any], side: str) -> bool:
    signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
    signed = _distance_from_open(row, signal)
    return (side == "UP" and signed > 0) or (side == "DOWN" and signed < 0)


def _recent_vol_usd(signal: dict[str, Any]) -> float:
    for key in (
        "resolved_pullback_recent_vol_floor_usd",
        "recent_vol_floor_usd",
        "spot_range_60s_usd",
    ):
        value = _safe_float(signal.get(key), -1.0)
        if value >= 0:
            return value
    return 0.0


def _passive_candidate(
    row: dict[str, Any],
    *,
    cfg: CurrentAlmostResolvedConfigV1,
    max_secs: int,
    min_distance_usd: float,
    min_distance_bps: float,
    min_score: int,
) -> tuple[bool, str, str, float, float]:
    signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
    ctx = row.get("current_scalp_context") if isinstance(row.get("current_scalp_context"), dict) else {}
    secs = _safe_int(row.get("current_secs"), _safe_int(signal.get("secs_to_end"), -1))
    if secs < cfg.extreme_99_min_secs_to_end or secs > max_secs:
        return False, "", "outside_time_window", 0.0, 0.0

    signed = _distance_from_open(row, signal)
    distance_bps = abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), abs(signed)))
    distance_usd = _safe_float(signal.get("distance_to_price_to_beat_usd"), 0.0)
    recent_vol = _recent_vol_usd(signal)
    market_range_30s = _safe_float(signal.get("market_range_30s"), _safe_float(ctx.get("market_range_30s"), 999.0))
    up_buy = _safe_float(signal.get("up_buy"), 0.0)
    down_buy = _safe_float(signal.get("down_buy"), 0.0)
    up_depth = _safe_float(signal.get("up_depth_top3_both_sides"), 0.0)
    down_depth = _safe_float(signal.get("down_depth_top3_both_sides"), 0.0)
    spot_5s = _safe_float(ctx.get("spot_delta_5s_bps"), _safe_float(signal.get("spot_delta_5s_bps"), 0.0))
    spot_15s = _safe_float(ctx.get("spot_delta_15s_bps"), _safe_float(signal.get("spot_delta_15s_bps"), 0.0))
    spot_30s = _safe_float(ctx.get("spot_delta_30s_bps"), _safe_float(signal.get("spot_delta_30s_bps"), 0.0))

    safe_distance_ok = (
        distance_usd >= min_distance_usd
        and distance_bps >= min_distance_bps
        and distance_usd >= recent_vol * cfg.passive_capture_min_distance_vs_recent_vol_mult
    )

    checks_common = [
        (safe_distance_ok, "safe_distance"),
        (market_range_30s <= cfg.passive_capture_max_market_range_30s, "market_range_30s"),
    ]

    up_score = _passive_capture_score(
        leader_price=up_buy,
        counter_price=down_buy,
        secs_to_end=secs,
        distance_usd=distance_usd,
        distance_bps=distance_bps,
        safe_distance_ok=safe_distance_ok,
        adverse_spot_bps=max(0.0, -spot_5s),
        market_range_30s=market_range_30s,
        depth=up_depth,
        cfg=cfg,
    )
    up_checks = checks_common + [
        (signed > 0, "direction"),
        (up_buy >= cfg.passive_capture_min_leader_price, "leader_price"),
        (down_buy <= cfg.passive_capture_max_counter_price, "counter_price"),
        (up_score >= min_score, "score"),
        (_safe_float(signal.get("up_price_to_beat_buffer_bps"), 0.0) >= cfg.near_end_min_price_to_beat_buffer_bps, "buffer_bps"),
        (_safe_float(signal.get("up_price_to_beat_buffer_usd"), 0.0) >= cfg.near_end_min_price_to_beat_buffer_usd, "buffer_usd"),
        (spot_5s >= -cfg.passive_capture_max_adverse_spot_5s_bps, "adverse_5s"),
        (spot_15s >= -cfg.passive_capture_max_adverse_spot_15s_bps, "adverse_15s"),
        (spot_30s >= -cfg.passive_capture_max_adverse_spot_30s_bps, "adverse_30s"),
    ]
    if all(ok for ok, _ in up_checks):
        return True, "UP", "passive_extended", up_buy, float(up_score)

    down_score = _passive_capture_score(
        leader_price=down_buy,
        counter_price=up_buy,
        secs_to_end=secs,
        distance_usd=distance_usd,
        distance_bps=distance_bps,
        safe_distance_ok=safe_distance_ok,
        adverse_spot_bps=max(0.0, spot_5s),
        market_range_30s=market_range_30s,
        depth=down_depth,
        cfg=cfg,
    )
    down_checks = checks_common + [
        (signed < 0, "direction"),
        (down_buy >= cfg.passive_capture_min_leader_price, "leader_price"),
        (up_buy <= cfg.passive_capture_max_counter_price, "counter_price"),
        (down_score >= min_score, "score"),
        (_safe_float(signal.get("down_price_to_beat_buffer_bps"), 0.0) >= cfg.near_end_min_price_to_beat_buffer_bps, "buffer_bps"),
        (_safe_float(signal.get("down_price_to_beat_buffer_usd"), 0.0) >= cfg.near_end_min_price_to_beat_buffer_usd, "buffer_usd"),
        (spot_5s <= cfg.passive_capture_max_adverse_spot_5s_bps, "adverse_5s"),
        (spot_15s <= cfg.passive_capture_max_adverse_spot_15s_bps, "adverse_15s"),
        (spot_30s <= cfg.passive_capture_max_adverse_spot_30s_bps, "adverse_30s"),
    ]
    if all(ok for ok, _ in down_checks):
        return True, "DOWN", "passive_extended", down_buy, float(down_score)

    failed = [name for ok, name in (up_checks if up_buy >= down_buy else down_checks) if not ok]
    return False, "UP" if up_buy >= down_buy else "DOWN", failed[0] if failed else "blocked", max(up_buy, down_buy), max(up_score, down_score)


def _tiered_min_distance(secs: int, schedule: list[tuple[int, float]]) -> float:
    for max_secs, min_distance in sorted(schedule, key=lambda item: item[0]):
        if secs <= max_secs:
            return float(min_distance)
    return float(schedule[-1][1]) if schedule else 80.0


def _tiered_vol_mult(secs: int, schedule: list[tuple[int, float]]) -> float:
    for max_secs, mult in sorted(schedule, key=lambda item: item[0]):
        if secs <= max_secs:
            return float(mult)
    return float(schedule[-1][1]) if schedule else 3.0


@dataclass
class Trade:
    scenario: str
    source_file: str
    slug: str
    side: str
    entry_secs: int
    entry_price: float
    leader_price: float
    distance_usd: float
    distance_bps: float
    score: float
    exit_secs: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_ticks: float | None = None


def _run_scenario(
    rows: list[dict[str, Any]],
    *,
    name: str,
    max_secs: int,
    min_distance_usd: float,
    min_distance_bps: float,
    min_score: int,
    stop_ticks: int,
) -> dict[str, Any]:
    cfg = CurrentAlmostResolvedConfigV1(passive_capture_max_secs=max_secs, passive_capture_min_safe_distance_usd=min_distance_usd)
    by_stream_slug: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        slug = str(row.get("current_slug") or (row.get("signal") or {}).get("current_slug") or "")
        if not slug:
            continue
        by_stream_slug.setdefault((str(row.get("_source_file")), slug), []).append(row)

    trades: list[Trade] = []
    blocked = Counter()
    for (source_file, slug), group in by_stream_slug.items():
        open_trade: Trade | None = None
        previous = None
        for row in group:
            signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
            secs = _safe_int(row.get("current_secs"), _safe_int(signal.get("secs_to_end"), 0))
            if open_trade is not None:
                bid = _side_bid(signal, open_trade.side)
                if bid > 0 and bid <= max(0.01, open_trade.entry_price - stop_ticks * 0.01):
                    open_trade.exit_secs = secs
                    open_trade.exit_price = round(bid, 6)
                    open_trade.exit_reason = "stop"
                    open_trade.pnl_ticks = round((open_trade.exit_price - open_trade.entry_price) / 0.01, 4)
                    trades.append(open_trade)
                    open_trade = None
                    break
                previous = row
                continue

            ok, side, reason, leader_price, score = _passive_candidate(
                row,
                cfg=cfg,
                max_secs=max_secs,
                min_distance_usd=min_distance_usd,
                min_distance_bps=min_distance_bps,
                min_score=min_score,
            )
            if ok and side in ("UP", "DOWN"):
                entry_price = round(max(0.01, min(0.99, leader_price - cfg.passive_capture_limit_ticks_below * 0.01)), 6)
                open_trade = Trade(
                    scenario=name,
                    source_file=source_file,
                    slug=slug,
                    side=side,
                    entry_secs=secs,
                    entry_price=entry_price,
                    leader_price=round(leader_price, 6),
                    distance_usd=round(_safe_float(signal.get("distance_to_price_to_beat_usd"), 0.0), 4),
                    distance_bps=round(abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), _distance_from_open(row, signal))), 4),
                    score=score,
                )
            else:
                blocked[reason] += 1
            previous = row

        if open_trade is not None:
            settle = previous or group[-1]
            win = _winner_for_side(settle, open_trade.side)
            open_trade.exit_secs = _safe_int(settle.get("current_secs"), 0)
            open_trade.exit_price = 1.0 if win else 0.0
            open_trade.exit_reason = "resolution_win" if win else "resolution_loss"
            open_trade.pnl_ticks = round((open_trade.exit_price - open_trade.entry_price) / 0.01, 4)
            trades.append(open_trade)

    pnls = [_safe_float(t.pnl_ticks) for t in trades]
    return {
        "scenario": name,
        "max_secs": max_secs,
        "min_distance_usd": min_distance_usd,
        "min_distance_bps": min_distance_bps,
        "min_score": min_score,
        "stop_ticks": stop_ticks,
        "trades": len(trades),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p < 0),
        "stops": sum(1 for t in trades if t.exit_reason == "stop"),
        "resolution_losses": sum(1 for t in trades if t.exit_reason == "resolution_loss"),
        "total_pnl_ticks": round(sum(pnls), 4),
        "avg_pnl_ticks": round(mean(pnls), 4) if pnls else None,
        "entry_secs_avg": round(mean([t.entry_secs for t in trades]), 2) if trades else None,
        "distance_usd_avg": round(mean([t.distance_usd for t in trades]), 2) if trades else None,
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "blocked_top": blocked.most_common(8),
        "sample_trades": [asdict(t) for t in trades[:20]],
    }


def _run_tiered_scenario(
    rows: list[dict[str, Any]],
    *,
    name: str,
    schedule: list[tuple[int, float]],
    min_distance_bps: float,
    min_score: int,
    stop_ticks: int,
) -> dict[str, Any]:
    max_secs = max(max_secs for max_secs, _ in schedule)
    cfg = CurrentAlmostResolvedConfigV1(passive_capture_max_secs=max_secs)
    by_stream_slug: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        slug = str(row.get("current_slug") or (row.get("signal") or {}).get("current_slug") or "")
        if not slug:
            continue
        by_stream_slug.setdefault((str(row.get("_source_file")), slug), []).append(row)

    trades: list[Trade] = []
    blocked = Counter()
    tier_hits = Counter()
    for (source_file, slug), group in by_stream_slug.items():
        open_trade: Trade | None = None
        previous = None
        for row in group:
            signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
            secs = _safe_int(row.get("current_secs"), _safe_int(signal.get("secs_to_end"), 0))
            if open_trade is not None:
                bid = _side_bid(signal, open_trade.side)
                if bid > 0 and bid <= max(0.01, open_trade.entry_price - stop_ticks * 0.01):
                    open_trade.exit_secs = secs
                    open_trade.exit_price = round(bid, 6)
                    open_trade.exit_reason = "stop"
                    open_trade.pnl_ticks = round((open_trade.exit_price - open_trade.entry_price) / 0.01, 4)
                    trades.append(open_trade)
                    open_trade = None
                    break
                previous = row
                continue

            min_distance_usd = _tiered_min_distance(secs, schedule)
            ok, side, reason, leader_price, score = _passive_candidate(
                row,
                cfg=cfg,
                max_secs=max_secs,
                min_distance_usd=min_distance_usd,
                min_distance_bps=min_distance_bps,
                min_score=min_score,
            )
            if ok and side in ("UP", "DOWN"):
                entry_price = round(max(0.01, min(0.99, leader_price - cfg.passive_capture_limit_ticks_below * 0.01)), 6)
                tier_hits[f"secs<={next(limit for limit, dist in sorted(schedule) if secs <= limit)}_dist>={int(min_distance_usd)}"] += 1
                open_trade = Trade(
                    scenario=name,
                    source_file=source_file,
                    slug=slug,
                    side=side,
                    entry_secs=secs,
                    entry_price=entry_price,
                    leader_price=round(leader_price, 6),
                    distance_usd=round(_safe_float(signal.get("distance_to_price_to_beat_usd"), 0.0), 4),
                    distance_bps=round(abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), _distance_from_open(row, signal))), 4),
                    score=score,
                )
            else:
                blocked[reason] += 1
            previous = row

        if open_trade is not None:
            settle = previous or group[-1]
            win = _winner_for_side(settle, open_trade.side)
            open_trade.exit_secs = _safe_int(settle.get("current_secs"), 0)
            open_trade.exit_price = 1.0 if win else 0.0
            open_trade.exit_reason = "resolution_win" if win else "resolution_loss"
            open_trade.pnl_ticks = round((open_trade.exit_price - open_trade.entry_price) / 0.01, 4)
            trades.append(open_trade)

    pnls = [_safe_float(t.pnl_ticks) for t in trades]
    return {
        "scenario": name,
        "schedule": [{"max_secs": max_secs, "min_distance_usd": dist} for max_secs, dist in schedule],
        "min_distance_bps": min_distance_bps,
        "min_score": min_score,
        "stop_ticks": stop_ticks,
        "trades": len(trades),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p < 0),
        "stops": sum(1 for t in trades if t.exit_reason == "stop"),
        "resolution_losses": sum(1 for t in trades if t.exit_reason == "resolution_loss"),
        "total_pnl_ticks": round(sum(pnls), 4),
        "avg_pnl_ticks": round(mean(pnls), 4) if pnls else None,
        "entry_secs_avg": round(mean([t.entry_secs for t in trades]), 2) if trades else None,
        "distance_usd_avg": round(mean([t.distance_usd for t in trades]), 2) if trades else None,
        "tier_hits": dict(tier_hits),
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "blocked_top": blocked.most_common(8),
        "sample_trades": [asdict(t) for t in trades[:20]],
    }


def _run_dynamic_distance_scenario(
    rows: list[dict[str, Any]],
    *,
    name: str,
    fixed_schedule: list[tuple[int, float]],
    vol_mult_schedule: list[tuple[int, float]],
    mode: str,
    min_distance_bps: float,
    min_score: int,
    stop_ticks: int,
) -> dict[str, Any]:
    max_secs = max([s for s, _ in fixed_schedule] + [s for s, _ in vol_mult_schedule])
    cfg = CurrentAlmostResolvedConfigV1(passive_capture_max_secs=max_secs)
    by_stream_slug: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        slug = str(row.get("current_slug") or (row.get("signal") or {}).get("current_slug") or "")
        if not slug:
            continue
        by_stream_slug.setdefault((str(row.get("_source_file")), slug), []).append(row)

    trades: list[Trade] = []
    blocked = Counter()
    tier_hits = Counter()
    min_distance_values: list[float] = []
    recent_vol_values: list[float] = []

    for (source_file, slug), group in by_stream_slug.items():
        open_trade: Trade | None = None
        previous = None
        for row in group:
            signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
            secs = _safe_int(row.get("current_secs"), _safe_int(signal.get("secs_to_end"), 0))
            if open_trade is not None:
                bid = _side_bid(signal, open_trade.side)
                if bid > 0 and bid <= max(0.01, open_trade.entry_price - stop_ticks * 0.01):
                    open_trade.exit_secs = secs
                    open_trade.exit_price = round(bid, 6)
                    open_trade.exit_reason = "stop"
                    open_trade.pnl_ticks = round((open_trade.exit_price - open_trade.entry_price) / 0.01, 4)
                    trades.append(open_trade)
                    open_trade = None
                    break
                previous = row
                continue

            fixed_min = _tiered_min_distance(secs, fixed_schedule)
            vol_mult = _tiered_vol_mult(secs, vol_mult_schedule)
            recent_vol = _recent_vol_usd(signal)
            vol_min = recent_vol * vol_mult
            if mode == "fixed":
                min_distance_usd = fixed_min
            elif mode == "vol":
                min_distance_usd = vol_min
            elif mode == "hybrid":
                min_distance_usd = max(fixed_min, vol_min)
            else:
                raise ValueError(f"unknown mode: {mode}")

            ok, side, reason, leader_price, score = _passive_candidate(
                row,
                cfg=cfg,
                max_secs=max_secs,
                min_distance_usd=min_distance_usd,
                min_distance_bps=min_distance_bps,
                min_score=min_score,
            )
            if ok and side in ("UP", "DOWN"):
                entry_price = round(max(0.01, min(0.99, leader_price - cfg.passive_capture_limit_ticks_below * 0.01)), 6)
                tier_hits[f"secs<={next(limit for limit, _ in sorted(fixed_schedule) if secs <= limit)}"] += 1
                min_distance_values.append(round(min_distance_usd, 4))
                recent_vol_values.append(round(recent_vol, 4))
                open_trade = Trade(
                    scenario=name,
                    source_file=source_file,
                    slug=slug,
                    side=side,
                    entry_secs=secs,
                    entry_price=entry_price,
                    leader_price=round(leader_price, 6),
                    distance_usd=round(_safe_float(signal.get("distance_to_price_to_beat_usd"), 0.0), 4),
                    distance_bps=round(abs(_safe_float(signal.get("distance_to_price_to_beat_bps"), _distance_from_open(row, signal))), 4),
                    score=score,
                )
            else:
                blocked[reason] += 1
            previous = row

        if open_trade is not None:
            settle = previous or group[-1]
            win = _winner_for_side(settle, open_trade.side)
            open_trade.exit_secs = _safe_int(settle.get("current_secs"), 0)
            open_trade.exit_price = 1.0 if win else 0.0
            open_trade.exit_reason = "resolution_win" if win else "resolution_loss"
            open_trade.pnl_ticks = round((open_trade.exit_price - open_trade.entry_price) / 0.01, 4)
            trades.append(open_trade)

    pnls = [_safe_float(t.pnl_ticks) for t in trades]
    return {
        "scenario": name,
        "mode": mode,
        "fixed_schedule": [{"max_secs": max_secs, "min_distance_usd": dist} for max_secs, dist in fixed_schedule],
        "vol_mult_schedule": [{"max_secs": max_secs, "vol_mult": mult} for max_secs, mult in vol_mult_schedule],
        "min_distance_bps": min_distance_bps,
        "min_score": min_score,
        "stop_ticks": stop_ticks,
        "trades": len(trades),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p < 0),
        "stops": sum(1 for t in trades if t.exit_reason == "stop"),
        "resolution_losses": sum(1 for t in trades if t.exit_reason == "resolution_loss"),
        "total_pnl_ticks": round(sum(pnls), 4),
        "avg_pnl_ticks": round(mean(pnls), 4) if pnls else None,
        "entry_secs_avg": round(mean([t.entry_secs for t in trades]), 2) if trades else None,
        "distance_usd_avg": round(mean([t.distance_usd for t in trades]), 2) if trades else None,
        "required_distance_avg": round(mean(min_distance_values), 2) if min_distance_values else None,
        "recent_vol_avg": round(mean(recent_vol_values), 2) if recent_vol_values else None,
        "tier_hits": dict(tier_hits),
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "blocked_top": blocked.most_common(8),
        "sample_trades": [asdict(t) for t in trades[:20]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze passive almost-resolved time-window extension")
    parser.add_argument("--logs-root", default="logs")
    parser.add_argument("--output", default="logs/research_base/almost_resolved_time_extension_v1.json")
    parser.add_argument("--min-score", type=int, default=85)
    parser.add_argument("--min-distance-bps", type=float, default=10.0)
    parser.add_argument("--stop-ticks", type=int, default=3)
    parser.add_argument("--tiered", action="store_true")
    parser.add_argument("--dynamic", action="store_true")
    args = parser.parse_args()

    files = _iter_raw_almost_logs(Path(args.logs_root))
    rows = _load_rows(files)
    scenarios = []
    for max_secs, distances in (
        (60, (80.0,)),
        (75, (80.0, 90.0, 100.0, 120.0)),
        (90, (80.0, 90.0, 100.0, 120.0, 150.0)),
    ):
        for distance in distances:
            scenarios.append(
                _run_scenario(
                    rows,
                    name=f"passive_max{max_secs}_dist{int(distance)}",
                    max_secs=max_secs,
                    min_distance_usd=distance,
                    min_distance_bps=float(args.min_distance_bps),
                    min_score=int(args.min_score),
                    stop_ticks=int(args.stop_ticks),
                )
            )
    if args.tiered:
        for name, schedule in (
            ("tiered_90_80_60_50", [(30, 50.0), (60, 60.0), (90, 80.0)]),
            ("tiered_90_100_60_70_30_50", [(30, 50.0), (60, 70.0), (90, 100.0)]),
            ("tiered_75_80_45_60_30_50", [(30, 50.0), (45, 60.0), (75, 80.0)]),
            ("tiered_90_120_75_90_45_70_30_50", [(30, 50.0), (45, 70.0), (75, 90.0), (90, 120.0)]),
        ):
            scenarios.append(
                _run_tiered_scenario(
                    rows,
                    name=name,
                    schedule=schedule,
                    min_distance_bps=float(args.min_distance_bps),
                    min_score=int(args.min_score),
                    stop_ticks=int(args.stop_ticks),
                )
            )
    if args.dynamic:
        fixed_schedule = [(30, 50.0), (60, 70.0), (90, 100.0)]
        vol_mult_schedule = [(30, 2.0), (60, 2.5), (90, 3.0)]
        scenarios.extend(
            [
                _run_dynamic_distance_scenario(
                    rows,
                    name="A_fixed_90_100_60_70_30_50",
                    fixed_schedule=fixed_schedule,
                    vol_mult_schedule=vol_mult_schedule,
                    mode="fixed",
                    min_distance_bps=float(args.min_distance_bps),
                    min_score=int(args.min_score),
                    stop_ticks=int(args.stop_ticks),
                ),
                _run_dynamic_distance_scenario(
                    rows,
                    name="B_vol_90_3x_60_2p5x_30_2x",
                    fixed_schedule=fixed_schedule,
                    vol_mult_schedule=vol_mult_schedule,
                    mode="vol",
                    min_distance_bps=float(args.min_distance_bps),
                    min_score=int(args.min_score),
                    stop_ticks=int(args.stop_ticks),
                ),
                _run_dynamic_distance_scenario(
                    rows,
                    name="C_hybrid_fixed_or_vol_max",
                    fixed_schedule=fixed_schedule,
                    vol_mult_schedule=vol_mult_schedule,
                    mode="hybrid",
                    min_distance_bps=float(args.min_distance_bps),
                    min_score=int(args.min_score),
                    stop_ticks=int(args.stop_ticks),
                ),
            ]
        )

    payload = {
        "files_scanned": len(files),
        "snapshot_rows": len(rows),
        "notes": [
            "Assumes passive fill at one tick below leader when criteria are met.",
            "Uses raw almost-resolved logs and simulates stop before resolution.",
            "This is a quality/risk replay, not an exact fill-rate replay.",
        ],
        "results": scenarios,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
