from __future__ import annotations

import argparse
import csv
import glob
import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


TICK = 0.01


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out):
            return default
        return out
    except (TypeError, ValueError):
        return default


def _side_prices(ctx: dict[str, Any], side: str) -> tuple[float, float]:
    side = side.lower()
    bid = _f(ctx.get(f"{side}_bid"))
    ask = _f(ctx.get(f"{side}_ask"))
    if bid <= 0 and ask > 0:
        bid = max(0.0, ask - TICK)
    if ask <= 0 and bid > 0:
        ask = min(1.0, bid + TICK)
    return round(bid, 4), round(ask, 4)


def _ctx(row: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(row.get("current_scalp_context"), dict):
        return row["current_scalp_context"]
    if isinstance(row.get("current_scalp"), dict):
        return row["current_scalp"]
    if isinstance(row.get("context_signal"), dict):
        # 15m shadow logs do not always include book prices, so these rows will
        # be ignored unless bid/ask fields are present.
        return row["context_signal"]
    return None


@dataclass(frozen=True)
class Snapshot:
    slug: str
    ts: float
    secs: int
    distance_bps: float
    spot_delta_5s: float
    spot_delta_15s: float
    market_delta_5s: float
    market_delta_15s: float
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float


@dataclass(frozen=True)
class ParamSet:
    entry_min: float
    entry_max: float
    min_abs_distance_bps: float
    min_spot_delta_15s_bps: float
    min_market_delta_15s: float
    max_secs_to_end: int
    min_secs_to_end: int
    initial_stop_ticks: int
    trail_ticks: int
    arm_after_ticks: int
    target_price: float

    def key(self) -> str:
        return (
            f"entry={self.entry_min:.2f}-{self.entry_max:.2f} "
            f"dist>={self.min_abs_distance_bps:g}bps "
            f"spot15>={self.min_spot_delta_15s_bps:g}bps "
            f"mkt15>={self.min_market_delta_15s:.2f} "
            f"secs={self.min_secs_to_end}-{self.max_secs_to_end} "
            f"stop={self.initial_stop_ticks} trail={self.trail_ticks} arm={self.arm_after_ticks}"
        )


@dataclass
class Trade:
    slug: str
    side: str
    entry_ts: float
    entry_secs: int
    entry_price: float
    exit_ts: float
    exit_secs: int
    exit_price: float
    exit_reason: str
    pnl_ticks: float
    best_bid: float
    final_distance_bps: float
    resolved_win: bool


def iter_snapshots(paths: Iterable[str]) -> list[Snapshot]:
    out: list[Snapshot] = []
    seen: set[tuple[str, float]] = set()
    for pattern in paths:
        for name in glob.glob(pattern, recursive=True):
            path = Path(name)
            if not path.is_file() or path.suffix.lower() != ".jsonl":
                continue
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("type") != "snapshot":
                        continue
                    ctx = _ctx(row)
                    if not ctx:
                        continue
                    slug = str(row.get("current_slug") or row.get("event_slug") or ctx.get("event_slug") or "")
                    ts = _f(row.get("ts"))
                    if not slug or ts <= 0:
                        continue
                    key = (slug, ts)
                    if key in seen:
                        continue
                    seen.add(key)
                    up_bid, up_ask = _side_prices(ctx, "up")
                    down_bid, down_ask = _side_prices(ctx, "down")
                    if min(up_bid, up_ask, down_bid, down_ask) <= 0:
                        continue
                    secs = int(_f(row.get("current_secs"), _f(ctx.get("secs_to_end"), -1)))
                    distance = _f(ctx.get("distance_from_open_bps"))
                    out.append(
                        Snapshot(
                            slug=slug,
                            ts=ts,
                            secs=secs,
                            distance_bps=distance,
                            spot_delta_5s=_f(ctx.get("spot_delta_5s_bps")),
                            spot_delta_15s=_f(ctx.get("spot_delta_15s_bps")),
                            market_delta_5s=_f(ctx.get("market_delta_5s")),
                            market_delta_15s=_f(ctx.get("market_delta_15s")),
                            up_bid=up_bid,
                            up_ask=up_ask,
                            down_bid=down_bid,
                            down_ask=down_ask,
                        )
                    )
    out.sort(key=lambda s: (s.slug, -s.secs, s.ts))
    return out


def group_by_slug(snaps: list[Snapshot]) -> dict[str, list[Snapshot]]:
    grouped: dict[str, list[Snapshot]] = {}
    for snap in snaps:
        grouped.setdefault(snap.slug, []).append(snap)
    return {slug: rows for slug, rows in grouped.items() if len(rows) >= 4}


def _trend_side(snap: Snapshot, params: ParamSet) -> str | None:
    if snap.secs > params.max_secs_to_end or snap.secs < params.min_secs_to_end:
        return None
    if abs(snap.distance_bps) < params.min_abs_distance_bps:
        return None
    side = "UP" if snap.distance_bps > 0 else "DOWN"
    direction = 1.0 if side == "UP" else -1.0
    if direction * snap.spot_delta_15s < params.min_spot_delta_15s_bps:
        return None
    if direction * snap.market_delta_15s < params.min_market_delta_15s:
        return None
    bid, ask = _side_prices(
        {
            "up_bid": snap.up_bid,
            "up_ask": snap.up_ask,
            "down_bid": snap.down_bid,
            "down_ask": snap.down_ask,
        },
        side,
    )
    if ask < params.entry_min or ask > params.entry_max:
        return None
    if ask - bid > 0.02:
        return None
    return side


def simulate_slug(rows: list[Snapshot], params: ParamSet) -> Trade | None:
    entry_i = -1
    side: str | None = None
    for i, snap in enumerate(rows):
        side = _trend_side(snap, params)
        if side:
            entry_i = i
            break
    if entry_i < 0 or not side:
        return None

    entry = rows[entry_i]
    entry_bid, entry_ask = (entry.up_bid, entry.up_ask) if side == "UP" else (entry.down_bid, entry.down_ask)
    entry_price = entry_ask
    best_bid = entry_bid
    stop_price = round(max(0.01, entry_price - params.initial_stop_ticks * TICK), 4)
    exit_price = entry_bid
    exit_reason = "end_of_log"
    exit_snap = entry

    for snap in rows[entry_i + 1 :]:
        bid = snap.up_bid if side == "UP" else snap.down_bid
        best_bid = max(best_bid, bid)
        if best_bid >= entry_price + params.arm_after_ticks * TICK:
            stop_price = max(stop_price, round(best_bid - params.trail_ticks * TICK, 4))
        if bid >= params.target_price:
            exit_price = bid
            exit_reason = "target"
            exit_snap = snap
            break
        if bid <= stop_price:
            exit_price = bid
            exit_reason = "trailing_stop"
            exit_snap = snap
            break
        if snap.secs <= 1:
            final_side_win = (side == "UP" and snap.distance_bps > 0) or (side == "DOWN" and snap.distance_bps < 0)
            exit_price = 1.0 if final_side_win else 0.0
            exit_reason = "resolution"
            exit_snap = snap
            break
        exit_price = bid
        exit_snap = snap

    final_distance = rows[-1].distance_bps
    resolved_win = (side == "UP" and final_distance > 0) or (side == "DOWN" and final_distance < 0)
    return Trade(
        slug=entry.slug,
        side=side,
        entry_ts=entry.ts,
        entry_secs=entry.secs,
        entry_price=entry_price,
        exit_ts=exit_snap.ts,
        exit_secs=exit_snap.secs,
        exit_price=round(exit_price, 4),
        exit_reason=exit_reason,
        pnl_ticks=round((exit_price - entry_price) / TICK, 2),
        best_bid=round(best_bid, 4),
        final_distance_bps=final_distance,
        resolved_win=resolved_win,
    )


def build_grid() -> list[ParamSet]:
    grid: list[ParamSet] = []
    for entry_min, entry_max in [(0.52, 0.72), (0.55, 0.78), (0.60, 0.82), (0.65, 0.88), (0.70, 0.92)]:
        for dist in [1.5, 2.5, 4.0, 6.0, 8.0]:
            for spot15 in [0.0, 0.5, 1.0]:
                for mkt15 in [0.0, 0.02, 0.04]:
                    for stop, trail, arm in [(3, 2, 2), (4, 3, 2), (6, 4, 3), (8, 5, 4)]:
                        grid.append(
                            ParamSet(
                                entry_min=entry_min,
                                entry_max=entry_max,
                                min_abs_distance_bps=dist,
                                min_spot_delta_15s_bps=spot15,
                                min_market_delta_15s=mkt15,
                                max_secs_to_end=170,
                                min_secs_to_end=20,
                                initial_stop_ticks=stop,
                                trail_ticks=trail,
                                arm_after_ticks=arm,
                                target_price=0.98,
                            )
                        )
    return grid


def summarize(params: ParamSet, trades: list[Trade]) -> dict[str, Any]:
    pnl = [t.pnl_ticks for t in trades]
    wins = [t for t in trades if t.pnl_ticks > 0]
    losses = [t for t in trades if t.pnl_ticks < 0]
    return {
        "params": params.key(),
        "trades": len(trades),
        "win_rate": round(100.0 * len(wins) / len(trades), 2) if trades else 0.0,
        "resolution_win_rate": round(100.0 * sum(1 for t in trades if t.resolved_win) / len(trades), 2) if trades else 0.0,
        "total_pnl_ticks": round(sum(pnl), 2),
        "avg_pnl_ticks": round(mean(pnl), 3) if pnl else 0.0,
        "avg_win_ticks": round(mean([t.pnl_ticks for t in wins]), 3) if wins else 0.0,
        "avg_loss_ticks": round(mean([t.pnl_ticks for t in losses]), 3) if losses else 0.0,
        "max_win_ticks": round(max(pnl), 2) if pnl else 0.0,
        "max_loss_ticks": round(min(pnl), 2) if pnl else 0.0,
        "targets": sum(1 for t in trades if t.exit_reason == "target"),
        "stops": sum(1 for t in trades if t.exit_reason == "trailing_stop"),
        "resolutions": sum(1 for t in trades if t.exit_reason == "resolution"),
        "end_of_log": sum(1 for t in trades if t.exit_reason == "end_of_log"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "paths",
        nargs="*",
        default=[
            "logs/dual_*/*.jsonl",
            "logs/current_almost_resolved*.jsonl",
            "logs/counter_reversal_*/*.jsonl",
        ],
    )
    parser.add_argument("--min-trades", type=int, default=8)
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--out-prefix", default="logs/trend_trailing_stop_sim_v1")
    args = parser.parse_args()

    snaps = iter_snapshots(args.paths)
    grouped = group_by_slug(snaps)
    rows: list[dict[str, Any]] = []
    best_trades: list[Trade] = []
    best_params: ParamSet | None = None
    for params in build_grid():
        trades = [trade for events in grouped.values() if (trade := simulate_slug(events, params))]
        if len(trades) < args.min_trades:
            continue
        summary = summarize(params, trades)
        rows.append(summary)
        if best_params is None or (
            summary["total_pnl_ticks"],
            summary["avg_pnl_ticks"],
            summary["trades"],
        ) > (
            rows[-2]["total_pnl_ticks"] if len(rows) > 1 else -999999,
            rows[-2]["avg_pnl_ticks"] if len(rows) > 1 else -999999,
            rows[-2]["trades"] if len(rows) > 1 else 0,
        ):
            pass

    rows.sort(key=lambda r: (r["total_pnl_ticks"], r["avg_pnl_ticks"], r["trades"]), reverse=True)
    if rows:
        best_key = rows[0]["params"]
        for params in build_grid():
            if params.key() == best_key:
                best_params = params
                best_trades = [trade for events in grouped.values() if (trade := simulate_slug(events, params))]
                break

    out_summary = Path(f"{args.out_prefix}_summary.csv")
    out_trades = Path(f"{args.out_prefix}_best_trades.csv")
    out_summary.parent.mkdir(parents=True, exist_ok=True)
    with out_summary.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["params"])
        writer.writeheader()
        writer.writerows(rows)
    if best_trades:
        with out_trades.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(best_trades[0].__dict__.keys()))
            writer.writeheader()
            writer.writerows([t.__dict__ for t in best_trades])

    print(f"snapshots={len(snaps)} events={len(grouped)} parameter_sets={len(rows)}")
    print(f"summary={out_summary}")
    if best_trades:
        print(f"best_trades={out_trades}")
    for row in rows[: args.top]:
        print(
            f"{row['total_pnl_ticks']:>7.1f} ticks | {row['trades']:>3} trades | "
            f"win={row['win_rate']:>5.1f}% | avg={row['avg_pnl_ticks']:>5.2f} | "
            f"max_loss={row['max_loss_ticks']:>5.1f} | stops={row['stops']:>3} | "
            f"target={row['targets']:>3} | {row['params']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
