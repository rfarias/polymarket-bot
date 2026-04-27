from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _round_files(root: Path) -> List[Path]:
    return sorted(root.glob("all_setups_paper*/round_*.jsonl"))


def _real_files(root: Path) -> List[Path]:
    return sorted(root.glob("next1_scalp_real_*/next1_scalp_real.jsonl"))


def _jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except Exception:
                continue


def _quote(snapshot: dict, side: str, kind: str) -> float:
    metrics = ((snapshot.get("next1_arb") or {}).get("metrics") or {})
    key = f"{side.lower()}_{kind}"
    return _safe_float(metrics.get(key), 0.0)


def _bucket_current_strength(current_up_mid: float) -> str:
    if current_up_mid >= 0.85 or current_up_mid <= 0.15:
        return "extreme"
    if current_up_mid >= 0.70 or current_up_mid <= 0.30:
        return "strong"
    if current_up_mid >= 0.60 or current_up_mid <= 0.40:
        return "medium"
    return "neutral"


def _bucket_price_skew(up_ask: float, down_ask: float) -> str:
    skew = abs(up_ask - down_ask)
    lo = min(up_ask, down_ask)
    hi = max(up_ask, down_ask)
    if lo <= 0.47 and hi >= 0.54:
        return "47_vs_54_plus"
    if lo <= 0.48 and hi >= 0.52:
        return "48_vs_52_plus"
    if lo >= 0.48 and hi <= 0.50:
        return "balanced_48_50"
    if lo >= 0.47 and hi <= 0.51:
        return "balanced_47_51"
    if skew >= 0.04:
        return "skew_4t"
    if skew >= 0.02:
        return "skew_2t"
    return "tight_center"


def _bucket_spot_regime(spot_15s_bps: float) -> str:
    if spot_15s_bps <= -5.0:
        return "down_5bps_plus"
    if spot_15s_bps <= -2.0:
        return "down_2_5bps"
    if spot_15s_bps < -0.5:
        return "down_0.5_2bps"
    if spot_15s_bps <= 0.5:
        return "flat_pm_0.5bps"
    if spot_15s_bps < 2.0:
        return "up_0.5_2bps"
    if spot_15s_bps < 5.0:
        return "up_2_5bps"
    return "up_5bps_plus"


def _trend_side(snapshot: dict) -> Optional[str]:
    signal = snapshot.get("next1_scalp") or {}
    current_up_mid = _safe_float(signal.get("current_up_mid"), -1.0)
    if current_up_mid < 0:
        return None
    if current_up_mid >= 0.60:
        return "UP"
    if current_up_mid <= 0.40:
        return "DOWN"
    return None


def _cheap_side(snapshot: dict) -> Optional[str]:
    up_ask = _quote(snapshot, "up", "ask")
    down_ask = _quote(snapshot, "down", "ask")
    if up_ask <= 0 or down_ask <= 0:
        return None
    return "UP" if up_ask < down_ask else "DOWN"


def _aligned_with_spot(snapshot: dict, side: str) -> bool:
    signal = snapshot.get("next1_scalp") or {}
    s5 = _safe_float(signal.get("spot_delta_5s_bps"), 0.0)
    s15 = _safe_float(signal.get("spot_delta_15s_bps"), 0.0)
    if side == "UP":
        return s5 > 0 and s15 > 0
    return s5 < 0 and s15 < 0


def _tick_size(snapshot: dict) -> float:
    signal = snapshot.get("next1_scalp") or {}
    spread_up = _safe_float(signal.get("spread_up"), 0.01)
    spread_down = _safe_float(signal.get("spread_down"), 0.01)
    tick = min(v for v in (spread_up, spread_down, 0.01) if v > 0)
    return max(0.01, min(0.01, tick))


@dataclass
class SimResult:
    exit_reason: str
    pnl_ticks: float
    hold_secs: float
    exit_price: float


def _simulate_trade(
    snapshots: List[dict],
    start_index: int,
    side: str,
    *,
    entry_kind: str = "ask",
    target_ticks: int = 2,
    stop_ticks: int = 2,
    max_hold_secs: int = 60,
) -> Optional[SimResult]:
    snap = snapshots[start_index]
    tick = _tick_size(snap)
    entry_price = _quote(snap, side.lower(), entry_kind)
    if entry_price <= 0:
        return None
    best_bid = _quote(snap, side.lower(), "bid")
    stop_price = round(max(0.01, entry_price - stop_ticks * tick), 6)
    target_price = round(min(0.99, entry_price + target_ticks * tick), 6)
    start_ts = _safe_float(snap.get("ts"), 0.0)

    for future in snapshots[start_index + 1 :]:
        future_ts = _safe_float(future.get("ts"), start_ts)
        hold_secs = max(0.0, future_ts - start_ts)
        bid_now = _quote(future, side.lower(), "bid")
        next1_secs = _safe_int(future.get("next1_secs"), 0)
        if bid_now > 0:
            best_bid = max(best_bid, bid_now)
            if best_bid >= round(entry_price + tick, 6):
                stop_price = max(stop_price, round(best_bid - tick, 6))
            if bid_now >= target_price:
                return SimResult("target", round((bid_now - entry_price) / tick, 4), hold_secs, bid_now)
            if bid_now <= stop_price:
                return SimResult("stop", round((bid_now - entry_price) / tick, 4), hold_secs, bid_now)
        if next1_secs and next1_secs <= 330:
            exit_price = bid_now if bid_now > 0 else best_bid
            return SimResult("deadline", round((exit_price - entry_price) / tick, 4), hold_secs, exit_price)
        if hold_secs >= max_hold_secs:
            exit_price = bid_now if bid_now > 0 else best_bid
            return SimResult("timeout", round((exit_price - entry_price) / tick, 4), hold_secs, exit_price)

    return None


def _summarize_bucket(rows: List[float]) -> Dict[str, float]:
    if not rows:
        return {"count": 0, "avg": 0.0, "win_rate": 0.0}
    wins = sum(1 for value in rows if value > 0)
    return {
        "count": len(rows),
        "avg": round(sum(rows) / len(rows), 4),
        "win_rate": round(wins / len(rows), 4),
    }


def analyze_paper(root: Path) -> dict:
    trade_rows: List[dict] = []
    head_to_head: List[dict] = []
    actual_by_reason = Counter()
    actual_by_setup = Counter()
    actual_by_bucket = Counter()
    snapshot_spot_regimes = Counter()

    for path in _round_files(root):
        rows = list(_jsonl(path))
        snapshots = [row for row in rows if row.get("type") == "snapshot" and row.get("next1_scalp")]
        if not snapshots:
            continue
        for snap in snapshots:
            signal = snap.get("next1_scalp") or {}
            s15 = signal.get("spot_delta_15s_bps")
            if s15 is not None:
                snapshot_spot_regimes[_bucket_spot_regime(_safe_float(s15, 0.0))] += 1

        active_trade_meta: Optional[dict] = None
        for row in rows:
            row_type = row.get("type")
            if row_type == "enter" and row.get("setup") == "next1_scalp":
                signal = row.get("signal") or {}
                active_trade_meta = {
                    "reason": str(signal.get("reason") or "unknown"),
                    "setup": str(signal.get("setup") or "unknown"),
                    "current_up_mid": _safe_float(signal.get("current_up_mid"), 0.5),
                    "spot_15s_bps": _safe_float(signal.get("spot_delta_15s_bps"), 0.0),
                    "spot_5s_bps": _safe_float(signal.get("spot_delta_5s_bps"), 0.0),
                    "side": str(signal.get("side") or "unknown"),
                }
                continue
            if row_type != "exit" or row.get("setup") != "next1_scalp":
                continue
            trade = row.get("trade") or {}
            meta = active_trade_meta or {
                "reason": "unknown",
                "setup": "unknown",
                "current_up_mid": 0.5,
                "spot_15s_bps": 0.0,
                "spot_5s_bps": 0.0,
                "side": "unknown",
            }
            current_up_mid = _safe_float(meta.get("current_up_mid"), 0.5)
            bucket = _bucket_current_strength(current_up_mid)
            reason = str(meta.get("reason") or "unknown")
            setup = str(meta.get("setup") or "unknown")
            actual_by_reason[reason] += 1
            actual_by_setup[setup] += 1
            actual_by_bucket[bucket] += 1
            trade_rows.append(
                {
                    "reason": reason,
                    "setup": setup,
                    "bucket": bucket,
                    "side": str(meta.get("side") or "unknown"),
                    "pnl_ticks": _safe_float(trade.get("pnl_ticks"), 0.0),
                    "entry_price": _safe_float(trade.get("entry_price"), 0.0),
                    "current_up_mid": current_up_mid,
                    "spot_15s_bps": _safe_float(meta.get("spot_15s_bps"), 0.0),
                    "spot_5s_bps": _safe_float(meta.get("spot_5s_bps"), 0.0),
                }
            )
            active_trade_meta = None

        first_contrast_index = None
        for idx, snap in enumerate(snapshots):
            signal = snap.get("next1_scalp") or {}
            trend_side = _trend_side(snap)
            cheap_side = _cheap_side(snap)
            if trend_side is None or cheap_side is None or trend_side == cheap_side:
                continue
            next1_secs = _safe_int(signal.get("next1_secs"), 0)
            if not 360 <= next1_secs <= 590:
                continue
            up_ask = _quote(snap, "up", "ask")
            down_ask = _quote(snap, "down", "ask")
            if up_ask <= 0 or down_ask <= 0:
                continue
            if max(_safe_float(signal.get("spread_up"), 0.0), _safe_float(signal.get("spread_down"), 0.0)) > 0.02:
                continue
            if _bucket_current_strength(_safe_float(signal.get("current_up_mid"), 0.5)) == "neutral":
                continue
            if first_contrast_index is None:
                first_contrast_index = idx
                break

        if first_contrast_index is None:
            continue

        snap = snapshots[first_contrast_index]
        signal = snap.get("next1_scalp") or {}
        trend_side = _trend_side(snap)
        cheap_side = _cheap_side(snap)
        follow = _simulate_trade(snapshots, first_contrast_index, trend_side)
        revert = _simulate_trade(snapshots, first_contrast_index, cheap_side)
        if not follow or not revert:
            continue
        head_to_head.append(
            {
                "file": path.name,
                "trend_side": trend_side,
                "cheap_side": cheap_side,
                "aligned_with_spot": _aligned_with_spot(snap, trend_side),
                "current_strength": _bucket_current_strength(_safe_float(signal.get("current_up_mid"), 0.5)),
                "price_skew": _bucket_price_skew(_quote(snap, "up", "ask"), _quote(snap, "down", "ask")),
                "current_up_mid": round(_safe_float(signal.get("current_up_mid"), 0.5), 4),
                "up_ask": _quote(snap, "up", "ask"),
                "down_ask": _quote(snap, "down", "ask"),
                "spot_delta_5s_bps": _safe_float(signal.get("spot_delta_5s_bps"), 0.0),
                "spot_delta_15s_bps": _safe_float(signal.get("spot_delta_15s_bps"), 0.0),
                "follow_pnl_ticks": follow.pnl_ticks,
                "follow_exit_reason": follow.exit_reason,
                "cheap_pnl_ticks": revert.pnl_ticks,
                "cheap_exit_reason": revert.exit_reason,
            }
        )

    actual_summary_by_reason = defaultdict(list)
    actual_summary_by_setup = defaultdict(list)
    actual_summary_by_bucket = defaultdict(list)
    actual_summary_by_spot = defaultdict(list)
    actual_summary_by_side_and_spot = defaultdict(list)
    for row in trade_rows:
        actual_summary_by_reason[row["reason"]].append(row["pnl_ticks"])
        actual_summary_by_setup[row["setup"]].append(row["pnl_ticks"])
        actual_summary_by_bucket[row["bucket"]].append(row["pnl_ticks"])
        spot_bucket = _bucket_spot_regime(_safe_float(row["spot_15s_bps"], 0.0))
        actual_summary_by_spot[spot_bucket].append(row["pnl_ticks"])
        actual_summary_by_side_and_spot[f'{row["side"]}|{spot_bucket}'].append(row["pnl_ticks"])

    follow_all = [row["follow_pnl_ticks"] for row in head_to_head]
    cheap_all = [row["cheap_pnl_ticks"] for row in head_to_head]
    by_skew_follow = defaultdict(list)
    by_skew_cheap = defaultdict(list)
    by_strength_follow = defaultdict(list)
    by_strength_cheap = defaultdict(list)
    aligned_follow = defaultdict(list)
    aligned_cheap = defaultdict(list)
    by_spot_regime_follow = defaultdict(list)
    by_spot_regime_cheap = defaultdict(list)
    for row in head_to_head:
        by_skew_follow[row["price_skew"]].append(row["follow_pnl_ticks"])
        by_skew_cheap[row["price_skew"]].append(row["cheap_pnl_ticks"])
        by_strength_follow[row["current_strength"]].append(row["follow_pnl_ticks"])
        by_strength_cheap[row["current_strength"]].append(row["cheap_pnl_ticks"])
        aligned_key = "spot_aligned" if row["aligned_with_spot"] else "spot_not_aligned"
        aligned_follow[aligned_key].append(row["follow_pnl_ticks"])
        aligned_cheap[aligned_key].append(row["cheap_pnl_ticks"])
        spot_bucket = _bucket_spot_regime(_safe_float(row["spot_delta_15s_bps"], 0.0))
        by_spot_regime_follow[spot_bucket].append(row["follow_pnl_ticks"])
        by_spot_regime_cheap[spot_bucket].append(row["cheap_pnl_ticks"])

    return {
        "paper_files": len(_round_files(root)),
        "actual_next1_trades": len(trade_rows),
        "snapshot_spot_regime_distribution": dict(sorted(snapshot_spot_regimes.items())),
        "actual_by_reason": {k: _summarize_bucket(v) for k, v in sorted(actual_summary_by_reason.items())},
        "actual_by_setup": {k: _summarize_bucket(v) for k, v in sorted(actual_summary_by_setup.items())},
        "actual_by_current_strength": {k: _summarize_bucket(v) for k, v in sorted(actual_summary_by_bucket.items())},
        "actual_by_spot_regime": {k: _summarize_bucket(v) for k, v in sorted(actual_summary_by_spot.items())},
        "actual_by_side_and_spot_regime": {k: _summarize_bucket(v) for k, v in sorted(actual_summary_by_side_and_spot.items())},
        "head_to_head_cases": len(head_to_head),
        "head_to_head_overall": {
            "follow_current": _summarize_bucket(follow_all),
            "buy_cheap_side": _summarize_bucket(cheap_all),
        },
        "head_to_head_by_price_skew": {
            bucket: {
                "follow_current": _summarize_bucket(by_skew_follow[bucket]),
                "buy_cheap_side": _summarize_bucket(by_skew_cheap[bucket]),
            }
            for bucket in sorted(set(by_skew_follow) | set(by_skew_cheap))
        },
        "head_to_head_by_current_strength": {
            bucket: {
                "follow_current": _summarize_bucket(by_strength_follow[bucket]),
                "buy_cheap_side": _summarize_bucket(by_strength_cheap[bucket]),
            }
            for bucket in sorted(set(by_strength_follow) | set(by_strength_cheap))
        },
        "head_to_head_by_spot_alignment": {
            bucket: {
                "follow_current": _summarize_bucket(aligned_follow[bucket]),
                "buy_cheap_side": _summarize_bucket(aligned_cheap[bucket]),
            }
            for bucket in sorted(set(aligned_follow) | set(aligned_cheap))
        },
        "head_to_head_by_spot_regime": {
            bucket: {
                "follow_current": _summarize_bucket(by_spot_regime_follow[bucket]),
                "buy_cheap_side": _summarize_bucket(by_spot_regime_cheap[bucket]),
            }
            for bucket in sorted(set(by_spot_regime_follow) | set(by_spot_regime_cheap))
        },
        "sample_cases": head_to_head[:12],
    }


def analyze_real(root: Path) -> dict:
    allow_counts = Counter()
    enter_counts = Counter()
    cancel_counts = Counter()
    filled_counts = Counter()

    for path in _real_files(root):
        last_enter_reason: Optional[str] = None
        for row in _jsonl(path):
            row_type = row.get("type")
            signal = row.get("signal") or {}
            reason = str(signal.get("reason") or "unknown")
            setup = str(signal.get("setup") or "unknown")
            key = f"{setup}|{reason}"
            if row_type == "snapshot" and signal.get("allow"):
                allow_counts[key] += 1
            elif row_type == "enter":
                enter_counts[key] += 1
                last_enter_reason = key
            elif row_type == "entry_cancel":
                cancel_counts[last_enter_reason or key] += 1
            elif row_type in ("exit", "startup_restore_exit"):
                filled_counts[last_enter_reason or key] += 1

    keys = sorted(set(allow_counts) | set(enter_counts) | set(cancel_counts) | set(filled_counts))
    return {
        key: {
            "allow_snapshots": allow_counts[key],
            "enters": enter_counts[key],
            "entry_cancels": cancel_counts[key],
            "completed_exits": filled_counts[key],
        }
        for key in keys
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze next1 historical logs for trend-follow vs buy-cheap regimes")
    parser.add_argument("--logs-root", type=str, default="logs", help="Logs root directory")
    parser.add_argument("--output", type=str, default=None, help="Optional JSON output path")
    args = parser.parse_args()

    root = Path(args.logs_root)
    report = {
        "paper_analysis": analyze_paper(root),
        "real_runner_summary": analyze_real(root),
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
