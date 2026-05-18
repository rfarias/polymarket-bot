"""
analyze_chart_context_on_logs.py

Replay BTC chart context filtering on existing paper trading logs.
Fetches historical Binance klines for each trade entry and computes
what the chart analysis would have said, then compares outcomes.

Usage:
    python analyze_chart_context_on_logs.py
    python analyze_chart_context_on_logs.py --logs logs/current_almost_resolved_paper_manual_eval_v1.jsonl
    python analyze_chart_context_on_logs.py --threshold 0.4
    python analyze_chart_context_on_logs.py --dir logs/all_setups_paper_until_20260427_0700_v2
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from market.btc_chart_context_v1 import BtcChartContext, fetch_btc_chart_context

# ---------------------------------------------------------------------------
# Penalty threshold: trades with penalty >= this are considered "filtered"
# ---------------------------------------------------------------------------
DEFAULT_PENALTY_THRESHOLD = 0.50


def _load_trades(path: Path) -> List[dict]:
    """
    Parse enter+exit pairs from a JSONL log file.
    Returns list of trade dicts with entry/exit details merged.
    The current_slug is tracked from snapshot events since enter events
    may not carry it directly.
    """
    pending: Dict[float, dict] = {}  # enter_ts -> enter event
    trades: List[dict] = []
    last_slug: str = ""
    last_ref_price: float = 0.0

    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue

            etype = ev.get("type")

            # Track slug and BTC price from snapshots
            if etype == "snapshot":
                last_slug = ev.get("current_slug") or last_slug
                ref = ev.get("reference") or {}
                rp = ref.get("reference_price") or 0.0
                if rp > 0:
                    last_ref_price = rp

            elif etype == "enter":
                ts = ev.get("ts") or 0.0
                ev["_slug"] = last_slug
                ev["_btc_price"] = last_ref_price
                pending[ts] = ev

            elif etype == "exit":
                trade = ev.get("trade") or {}
                created_at = trade.get("created_at")
                enter_ev = pending.pop(created_at, None)
                if enter_ev is None:
                    continue

                sig = enter_ev.get("signal") or {}
                slug = enter_ev.get("_slug") or enter_ev.get("current_slug") or ""
                btc_price = enter_ev.get("_btc_price") or _infer_btc_price(sig)
                event_end_ts = _slug_to_end_ts(slug)

                trades.append(
                    {
                        "enter_ts": enter_ev.get("ts") or 0.0,
                        "exit_ts": ev.get("ts") or 0.0,
                        "side": sig.get("side"),
                        "entry_price": sig.get("entry_price"),
                        "exit_price": trade.get("exit_price"),
                        "exit_reason": trade.get("exit_reason"),
                        "pnl_ticks": trade.get("pnl_ticks"),
                        "setup_variant": sig.get("setup_variant"),
                        "btc_price": btc_price,
                        "event_end_ts": event_end_ts,
                        "slug": slug,
                        "source_file": path.name,
                    }
                )

    return trades


def _infer_btc_price(sig: dict) -> float:
    """
    Estimate BTC price from signal fields.
    Uses opening_reference_price + distance_from_open_bps when available.
    """
    op = sig.get("opening_reference_price") or 0.0
    dist_bps = sig.get("distance_to_price_to_beat_bps") or 0.0
    if op > 0 and dist_bps > 0:
        side = sig.get("side") or ""
        if side == "UP":
            return op * (1 + dist_bps / 10_000.0)
        elif side == "DOWN":
            return op * (1 - dist_bps / 10_000.0)
        return op
    return 0.0


def _slug_to_end_ts(slug: str) -> float:
    """
    Parse event end time from slug like 'btc-updown-5m-1777126200'.
    The number is the market START time; end = start + 300.
    """
    try:
        return float(slug.split("-")[-1]) + 300.0
    except Exception:
        return 0.0


def _batch_fetch_contexts(
    trades: List[dict],
    *,
    rate_limit_delay: float = 0.25,
) -> List[Optional[BtcChartContext]]:
    """
    Fetch chart context for each trade.
    Groups trades by 5min bucket to avoid duplicate API calls.
    Returns a list aligned with trades.
    """
    # Group by (5min_bucket, side) to reuse fetched contexts
    bucket_cache: Dict[int, BtcChartContext] = {}
    results: List[Optional[BtcChartContext]] = []

    total = len(trades)
    for i, t in enumerate(trades):
        entry_ts = t["enter_ts"]
        bucket = int(entry_ts / 300)

        print(f"  [{i+1}/{total}] ts={entry_ts:.0f}  slug={t['slug']}  side={t['side']}  pnl={t['pnl_ticks']}", end="", flush=True)

        if bucket in bucket_cache:
            ctx = bucket_cache[bucket]
            print(f"  [cache]")
        else:
            # end_ms: last ms before current 5min candle opened
            end_ms = bucket * 300 * 1000 - 1
            btc_price = t["btc_price"] or 80_000.0
            event_end_ts = t["event_end_ts"] or entry_ts + 300

            try:
                ctx = fetch_btc_chart_context(
                    current_price=btc_price,
                    entry_ts=entry_ts,
                    event_end_ts=event_end_ts,
                    end_ms_override=end_ms,
                )
                time.sleep(rate_limit_delay)
                print(f"  trend={ctx.trend_5m}  pattern={ctx.last_candle_pattern}  15m_aligned={ctx.is_15m_aligned}")
            except Exception as exc:
                print(f"  ERROR: {exc}")
                ctx = BtcChartContext(
                    trend_5m="NEUTRAL", ema9_5m=0, ema21_5m=0,
                    at_resistance=False, at_support=False,
                    resistance_level=None, support_level=None,
                    last_candle_pattern="neutral", last_candle_bias=0.0,
                    is_15m_aligned=False, trend_15m=None,
                    ema9_15m=None, ema21_15m=None,
                    btc_price=btc_price, timestamp=entry_ts,
                    fetch_error=True,
                )

            bucket_cache[bucket] = ctx

        results.append(ctx)

    return results


def _print_stats(trades: List[dict], contexts: List[Optional[BtcChartContext]], threshold: float) -> None:
    """Print comparison stats: original vs filtered."""

    all_wins = 0
    all_losses = 0
    pass_wins = 0
    pass_losses = 0
    filtered_wins = 0
    filtered_losses = 0

    by_variant_original: Dict[str, Tuple[int, int]] = defaultdict(lambda: (0, 0))
    by_variant_filtered: Dict[str, Tuple[int, int]] = defaultdict(lambda: (0, 0))

    penalty_dist: List[float] = []
    detail_rows: List[dict] = []

    for t, ctx in zip(trades, contexts):
        pnl = t.get("pnl_ticks") or 0.0
        is_win = pnl > 0
        variant = t.get("setup_variant") or "unknown"

        if is_win:
            all_wins += 1
        else:
            all_losses += 1

        # per-variant original
        w, l = by_variant_original[variant]
        by_variant_original[variant] = (w + (1 if is_win else 0), l + (0 if is_win else 1))

        penalty = ctx.get_penalty_for_side(t.get("side") or "") if ctx else 0.0
        penalty_dist.append(penalty)
        would_filter = penalty >= threshold

        row = {
            "ts": t["enter_ts"],
            "side": t["side"],
            "variant": variant,
            "pnl_ticks": pnl,
            "is_win": is_win,
            "penalty": penalty,
            "filtered": would_filter,
            "trend_5m": ctx.trend_5m if ctx else "?",
            "pattern": ctx.last_candle_pattern if ctx else "?",
            "15m_aligned": ctx.is_15m_aligned if ctx else False,
            "trend_15m": ctx.trend_15m if ctx else None,
        }
        detail_rows.append(row)

        if would_filter:
            if is_win:
                filtered_wins += 1
            else:
                filtered_losses += 1
        else:
            if is_win:
                pass_wins += 1
            else:
                pass_losses += 1
            w, l = by_variant_filtered[variant]
            by_variant_filtered[variant] = (w + (1 if is_win else 0), l + (0 if is_win else 1))

    total = all_wins + all_losses
    passed = pass_wins + pass_losses
    filtered = filtered_wins + filtered_losses

    def wr(wins: int, total: int) -> str:
        if total == 0:
            return "N/A"
        return f"{wins/total*100:.1f}%  ({wins}W/{total-wins}L/{total}T)"

    print("\n" + "=" * 65)
    print("CHART CONTEXT REPLAY ANALYSIS")
    print(f"Penalty threshold: {threshold:.2f}")
    print("=" * 65)

    print(f"\n{'ORIGINAL (all trades)':<30} {wr(all_wins, total)}")
    print(f"{'PASSED (penalty < threshold)':<30} {wr(pass_wins, passed)}")
    print(f"{'FILTERED (penalty >= threshold)':<30} {wr(filtered_wins, filtered)}")

    if filtered > 0:
        print(f"\n  Trades filtered out : {filtered}  ({filtered/total*100:.1f}% of total)")
        print(f"  Filtered win rate   : {wr(filtered_wins, filtered)}")
        print(f"  Net gain from filter: {filtered_losses - filtered_wins:+d} losses avoided, {-(filtered_wins)} wins lost")

    # Penalty distribution
    if penalty_dist:
        below_25 = sum(1 for p in penalty_dist if p < 0.25)
        below_50 = sum(1 for p in penalty_dist if 0.25 <= p < 0.50)
        above_50 = sum(1 for p in penalty_dist if p >= 0.50)
        print(f"\n  Penalty distribution:")
        print(f"    < 0.25 (with-trend)    : {below_25} trades")
        print(f"    0.25–0.50 (mild counter): {below_50} trades")
        print(f"    >= 0.50 (strong counter): {above_50} trades")

    # Per-variant breakdown
    print("\n  By setup variant (passed trades only):")
    for variant in sorted(set(list(by_variant_original) + list(by_variant_filtered))):
        ow, ol = by_variant_original.get(variant, (0, 0))
        fw, fl = by_variant_filtered.get(variant, (0, 0))
        orig_wr = f"{ow/(ow+ol)*100:.0f}%" if (ow + ol) > 0 else "N/A"
        filt_wr = f"{fw/(fw+fl)*100:.0f}%" if (fw + fl) > 0 else "N/A"
        print(f"    {variant:<45}  orig={orig_wr}  filtered={filt_wr}  ({ow+ol}->{fw+fl} trades)")

    # Trend breakdown for filtered losses
    counter_trend_losses = [r for r in detail_rows if r["filtered"] and not r["is_win"]]
    if counter_trend_losses:
        print(f"\n  Counter-trend losses avoided ({len(counter_trend_losses)}):")
        trend_counts: Dict[str, int] = defaultdict(int)
        pattern_counts: Dict[str, int] = defaultdict(int)
        for r in counter_trend_losses:
            trend_counts[r["trend_5m"]] += 1
            pattern_counts[r["pattern"]] += 1
        for k, v in sorted(trend_counts.items(), key=lambda x: -x[1]):
            print(f"    trend_5m={k}: {v}")
        for k, v in sorted(pattern_counts.items(), key=lambda x: -x[1])[:5]:
            print(f"    pattern={k}: {v}")

    # 15min aligned trades
    aligned = [r for r in detail_rows if r["15m_aligned"]]
    if aligned:
        al_wins = sum(1 for r in aligned if r["is_win"])
        print(f"\n  15min-aligned trades: {len(aligned)}  win rate={wr(al_wins, len(aligned))}")

    print("\n  Detail (filtered trades):")
    print(f"  {'ts':<15} {'side':<6} {'pnl':>5} {'penalty':>7}  {'trend':<8} {'pattern'}")
    print("  " + "-" * 70)
    for r in sorted(detail_rows, key=lambda x: -x["penalty"])[:30]:
        mark = "FILT" if r["filtered"] else "    "
        print(
            f"  {r['ts']:<15.0f} {r['side'] or '?':<6} {r['pnl_ticks']:>5.1f}"
            f" {r['penalty']:>7.2f}  {r['trend_5m']:<8} {r['pattern']}  {mark}"
        )

    print("=" * 65)


def _collect_jsonl_files(args_logs: List[str], args_dir: Optional[str]) -> List[Path]:
    files: List[Path] = []
    for lp in (args_logs or []):
        p = Path(lp)
        if p.exists():
            files.append(p)

    if args_dir:
        d = Path(args_dir)
        files.extend(sorted(d.glob("**/*.jsonl")))

    if not files:
        # Default: scan known log files
        base = Path("logs")
        defaults = [
            base / "current_almost_resolved_paper_manual_eval_v1.jsonl",
            base / "current_almost_resolved_paper_manual_ext_600s.jsonl",
        ]
        for p in defaults:
            if p.exists():
                files.append(p)

    return files


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay chart context filtering on paper trade logs")
    parser.add_argument("--logs", nargs="+", default=[], help="Specific JSONL log file(s)")
    parser.add_argument("--dir", default=None, help="Directory to scan for JSONL files")
    parser.add_argument("--threshold", type=float, default=DEFAULT_PENALTY_THRESHOLD,
                        help=f"Penalty threshold for filtering (default {DEFAULT_PENALTY_THRESHOLD})")
    parser.add_argument("--delay", type=float, default=0.25,
                        help="Seconds between Binance API calls (default 0.25)")
    args = parser.parse_args()

    files = _collect_jsonl_files(args.logs, args.dir)
    if not files:
        print("No JSONL files found. Use --logs or --dir.")
        return

    print(f"Found {len(files)} log file(s):")
    for f in files:
        print(f"  {f}")

    all_trades: List[dict] = []
    for f in files:
        t = _load_trades(f)
        print(f"  {f.name}: {len(t)} trades")
        all_trades.extend(t)

    if not all_trades:
        print("No enter/exit trade pairs found in the log files.")
        return

    print(f"\nFetching Binance chart context for {len(all_trades)} trades...")
    contexts = _batch_fetch_contexts(all_trades, rate_limit_delay=args.delay)

    _print_stats(all_trades, contexts, threshold=args.threshold)


if __name__ == "__main__":
    main()
