"""
analyze_all_setups_research_base.py

Analisa todos os setups no research_base para encontrar filtros de entrada de alta qualidade.
Mesma metodologia que analyze_entry_quality_on_logs.py, adaptada para o formato flat do research_base.

Formato research_base (events):
- enter: {setup, setup_variant, side, entry_price, secs_to_end, market_range_30s,
           distance_to_price_to_beat_bps, spot_delta_5s_bps, spot_delta_15s_bps, ...}
- exit:  {entry_price, exit_price, pnl_ticks, exit_reason/reason}
Pairing: sequential — cada enter é seguido pelo seu exit dentro do mesmo source_file.

Usage:
    python analyze_all_setups_research_base.py
    python analyze_all_setups_research_base.py --setup almost_resolved
    python analyze_all_setups_research_base.py --setup continuation_soft --verbose
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8")

RESEARCH_BASE = Path("logs/research_base/research_events_all_v1.jsonl")

EXIT_TYPES = {"exit", "flat", "redeem_flat", "redeem_dust_archived", "external_close_detected"}


def _sf(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class Trade:
    def __init__(self, enter_ev: dict):
        self.setup = enter_ev.get("setup", "")
        self.setup_variant = enter_ev.get("setup_variant", "") or ""
        self.side = enter_ev.get("side", "")
        self.entry_price = _sf(enter_ev.get("entry_price"))
        self.secs_to_end = _sf(enter_ev.get("secs_to_end"), -1)
        self.market_range_30s = _sf(enter_ev.get("market_range_30s"))
        self.dist_to_beat_bps = _sf(enter_ev.get("distance_to_price_to_beat_bps"))
        self.spot_delta_5s = _sf(enter_ev.get("spot_delta_5s_bps"))
        self.spot_delta_15s = _sf(enter_ev.get("spot_delta_15s_bps"))
        self.spot_delta_30s = _sf(enter_ev.get("spot_delta_30s_bps"))
        self.enter_reason = enter_ev.get("reason", "")
        self.source_file = enter_ev.get("source_file", "")
        self.ts = _sf(enter_ev.get("ts"))

        # filled from exit event
        self.exit_price: Optional[float] = None
        self.exit_reason: str = ""
        self.pnl_ticks: Optional[float] = None

    @property
    def is_win(self) -> bool:
        return self.pnl_ticks is not None and self.pnl_ticks > 0

    @property
    def is_loss(self) -> bool:
        return self.pnl_ticks is not None and self.pnl_ticks < 0

    @property
    def exit_dist(self) -> float:
        """Distance from entry to target.
        For target exits: exit_price - entry_price (both UP and DOWN tokens bought low, target high).
        For losses (no target exit): approximate as 0.99 - entry_price for almost_resolved.
        """
        if self.exit_price is not None and self.exit_reason in ("target", "resolution"):
            # Both UP and DOWN token targets are above entry price
            return round(self.exit_price - self.entry_price, 4)
        if self.setup == "almost_resolved":
            # Approximate: target is ~0.99 for both UP and DOWN sides
            return round(0.99 - self.entry_price, 4)
        return 0.0


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_research_base(path: Path) -> List[Trade]:
    """Parse enter+exit pairs from research_base flat-format JSONL."""
    trades: List[Trade] = []
    pending_by_file: Dict[str, List[Trade]] = defaultdict(list)

    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue

            etype = ev.get("type", "")
            src = ev.get("source_file", "")

            if etype == "enter":
                trade = Trade(ev)
                pending_by_file[src].append(trade)
                trades.append(trade)

            elif etype in EXIT_TYPES or etype == "exit":
                # Match to the oldest pending trade from same source_file with matching entry_price
                entry_p = _sf(ev.get("entry_price"))
                candidates = pending_by_file.get(src, [])
                matched = None
                for t in candidates:
                    if t.pnl_ticks is None and abs(t.entry_price - entry_p) < 0.001:
                        matched = t
                        break
                if matched is None and candidates:
                    # Fallback: oldest unmatched from same file
                    for t in candidates:
                        if t.pnl_ticks is None:
                            matched = t
                            break
                if matched:
                    matched.exit_price = _sf(ev.get("exit_price"))
                    matched.exit_reason = str(ev.get("exit_reason") or ev.get("reason") or "")
                    matched.pnl_ticks = ev.get("pnl_ticks")

    return trades


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _stats(trades: List[Trade]) -> dict:
    with_pnl = [t for t in trades if t.pnl_ticks is not None]
    if not with_pnl:
        return {"n": 0, "wr": 0, "avg": 0, "total": 0, "worst": 0, "wins": 0, "losses": 0}
    wins = [t for t in with_pnl if t.pnl_ticks > 0]
    losses = [t for t in with_pnl if t.pnl_ticks <= 0]
    n = len(with_pnl)
    return {
        "n": n,
        "wr": len(wins) / n * 100,
        "avg": sum(t.pnl_ticks for t in with_pnl) / n,
        "total": sum(t.pnl_ticks for t in with_pnl),
        "worst": min((t.pnl_ticks for t in with_pnl), default=0),
        "wins": len(wins),
        "losses": len(losses),
    }


def _fmt_stats(s: dict) -> str:
    if s["n"] == 0:
        return "N/A"
    return (
        f"N={s['n']:3d}  WR={s['wr']:5.1f}%  ({s['wins']}W/{s['losses']}L)  "
        f"avg={s['avg']:+.2f}tk  total={s['total']:+.1f}tk  worst={s['worst']:+.1f}tk"
    )


# ---------------------------------------------------------------------------
# Per-setup analysis
# ---------------------------------------------------------------------------

def _analyze_setup(setup: str, trades: List[Trade], verbose: bool = False) -> None:
    if not trades:
        print(f"\n{setup}: no trades found.")
        return

    s = _stats(trades)
    print(f"\n{'='*70}")
    print(f"SETUP: {setup}  ({s['n']} trades)")
    print(f"{'='*70}")
    print(f"Overall:  {_fmt_stats(s)}")

    # Exit reason breakdown
    reasons = defaultdict(list)
    for t in trades:
        if t.pnl_ticks is not None:
            reasons[t.exit_reason or "?"].append(t)
    if reasons:
        print("\nExit reasons:")
        for r, ts in sorted(reasons.items(), key=lambda x: -len(x[1])):
            rs = _stats(ts)
            print(f"  {r:30s} {_fmt_stats(rs)}")

    # Variant breakdown (if multiple)
    variants = defaultdict(list)
    for t in trades:
        variants[t.setup_variant or "(none)"].append(t)
    if len(variants) > 1:
        print("\nVariants:")
        for v, ts in sorted(variants.items(), key=lambda x: -len(x[1])):
            vs = _stats(ts)
            print(f"  {v:35s} {_fmt_stats(vs)}")

    # Segmentation by setup
    if setup == "almost_resolved":
        _segment_almost_resolved(trades, verbose)
    elif setup in ("continuation_soft", "continuation", "continuation_extreme"):
        _segment_continuation(setup, trades, verbose)
    elif setup == "reversal":
        _segment_reversal(trades, verbose)
    elif setup == "current_trend_trailing_v1":
        _segment_trend_trailing(trades, verbose)
    elif setup == "counter_reversal_near_resolved":
        _segment_counter_reversal(trades, verbose)

    # Losses detail
    losses = [t for t in trades if t.pnl_ticks is not None and t.pnl_ticks < 0]
    if losses:
        print(f"\nLosses ({len(losses)}):")
        for t in sorted(losses, key=lambda x: x.pnl_ticks):
            print(
                f"  pnl={t.pnl_ticks:+.1f}tk  entry={t.entry_price:.3f}  "
                f"exit={f'{t.exit_price:.3f}' if t.exit_price else '?'}  "
                f"secs={t.secs_to_end:.0f}  side={t.side}  "
                f"exit_dist={t.exit_dist:.3f}  range30s={t.market_range_30s:.3f}  "
                f"variant={t.setup_variant}  exit_reason={t.exit_reason}"
            )


def _segment_almost_resolved(trades: List[Trade], verbose: bool) -> None:
    """Segmentation for almost_resolved using exit_dist and entry_price."""
    print("\n--- Segmentation: exit_dist (distance entry→target) ---")
    thresholds = [
        ("exit_dist < 0.01  (same tick as target)", lambda t: t.exit_dist < 0.01),
        ("exit_dist < 0.02  (1 tick from target)", lambda t: t.exit_dist < 0.02),
        ("exit_dist 0.02-0.04", lambda t: 0.02 <= t.exit_dist < 0.04),
        ("exit_dist >= 0.04  (4+ ticks away)", lambda t: t.exit_dist >= 0.04),
    ]
    for label, fn in thresholds:
        seg = [t for t in trades if fn(t)]
        print(f"  {label:45s} {_fmt_stats(_stats(seg))}")

    print("\n--- Segmentation: entry_price ---")
    ep_thresholds = [
        ("entry >= 0.99", lambda t: t.entry_price >= 0.99),
        ("entry >= 0.98", lambda t: 0.98 <= t.entry_price < 0.99),
        ("entry >= 0.97", lambda t: 0.97 <= t.entry_price < 0.98),
        ("entry >= 0.95", lambda t: 0.95 <= t.entry_price < 0.97),
        ("entry < 0.95", lambda t: t.entry_price < 0.95),
    ]
    for label, fn in ep_thresholds:
        seg = [t for t in trades if fn(t)]
        print(f"  {label:45s} {_fmt_stats(_stats(seg))}")

    print("\n--- Segmentation: secs_to_end ---")
    secs_thresholds = [
        ("secs < 30", lambda t: 0 <= t.secs_to_end < 30),
        ("secs 30-60", lambda t: 30 <= t.secs_to_end < 60),
        ("secs 60-120", lambda t: 60 <= t.secs_to_end < 120),
        ("secs 120-180", lambda t: 120 <= t.secs_to_end < 180),
        ("secs >= 180", lambda t: t.secs_to_end >= 180),
    ]
    for label, fn in secs_thresholds:
        seg = [t for t in trades if fn(t)]
        print(f"  {label:45s} {_fmt_stats(_stats(seg))}")

    print("\n--- Segmentation: market_range_30s (volatility) ---")
    for thr in [0.0, 0.01, 0.02, 0.04]:
        if thr == 0.0:
            seg = [t for t in trades if t.market_range_30s == 0.0]
            label = "range30s == 0  (calm)"
        else:
            prev = [0.0, 0.01, 0.02][([0.0, 0.01, 0.02, 0.04].index(thr) - 1)]
            seg = [t for t in trades if prev < t.market_range_30s <= thr]
            label = f"range30s ({prev:.2f}, {thr:.2f}]"
        print(f"  {label:45s} {_fmt_stats(_stats(seg))}")
    seg = [t for t in trades if t.market_range_30s > 0.04]
    print(f"  {'range30s > 0.04  (volatile)':45s} {_fmt_stats(_stats(seg))}")

    print("\n--- Combined: exit_dist < 0.02 AND entry >= 0.97 ---")
    seg = [t for t in trades if t.exit_dist < 0.02 and t.entry_price >= 0.97]
    print(f"  {'T1: exit_dist<0.02 + entry>=0.97':45s} {_fmt_stats(_stats(seg))}")
    seg2 = [t for t in trades if t.exit_dist < 0.02 and t.entry_price < 0.97]
    print(f"  {'T1 miss: exit_dist<0.02 but entry<0.97':45s} {_fmt_stats(_stats(seg2))}")

    if verbose:
        print("\n--- distance_to_price_to_beat_bps distribution ---")
        dist_vals = [(t.dist_to_beat_bps, t.pnl_ticks) for t in trades if t.pnl_ticks is not None]
        for thr in [5, 10, 15, 20, 30, 50]:
            seg = [(d, p) for d, p in dist_vals if d <= thr]
            wins = sum(1 for _, p in seg if p > 0)
            print(f"  dist_to_beat <= {thr:2d} bps: N={len(seg):3d}  WR={wins/len(seg)*100:.1f}%" if seg else f"  dist_to_beat <= {thr:2d} bps: N=0")


def _segment_continuation(setup: str, trades: List[Trade], verbose: bool) -> None:
    """Segmentation for continuation setups using entry_price, secs_to_end, momentum."""
    print("\n--- Segmentation: entry_price (how far from 0.50 midpoint) ---")
    # These are next1 scalp - entry_price near 0.50 = uncertain, near edges = trend strong
    for label, fn in [
        ("entry 0.48-0.52 (near midpoint)", lambda t: 0.48 <= t.entry_price <= 0.52),
        ("entry 0.45-0.55 (mid zone)", lambda t: 0.45 <= t.entry_price <= 0.55),
        ("entry 0.40-0.44 or 0.56-0.60 (extended)", lambda t: (0.40 <= t.entry_price <= 0.44) or (0.56 <= t.entry_price <= 0.60)),
        ("entry < 0.40 or > 0.60 (strong trend)", lambda t: t.entry_price < 0.40 or t.entry_price > 0.60),
    ]:
        seg = [t for t in trades if fn(t)]
        print(f"  {label:50s} {_fmt_stats(_stats(seg))}")

    print("\n--- Segmentation: secs_to_end ---")
    for label, fn in [
        ("secs < 60", lambda t: 0 <= t.secs_to_end < 60),
        ("secs 60-120", lambda t: 60 <= t.secs_to_end < 120),
        ("secs 120-200", lambda t: 120 <= t.secs_to_end < 200),
        ("secs >= 200", lambda t: t.secs_to_end >= 200),
    ]:
        seg = [t for t in trades if fn(t)]
        print(f"  {label:50s} {_fmt_stats(_stats(seg))}")

    print("\n--- Segmentation: spot_delta_5s_bps (momentum at entry) ---")
    for label, fn in [
        ("delta5s == 0  (no momentum)", lambda t: t.spot_delta_5s == 0.0),
        ("0 < delta5s <= 1.0  (weak)", lambda t: 0 < t.spot_delta_5s <= 1.0),
        ("1.0 < delta5s <= 3.0  (moderate)", lambda t: 1.0 < t.spot_delta_5s <= 3.0),
        ("delta5s > 3.0  (strong)", lambda t: t.spot_delta_5s > 3.0),
        ("delta5s negative  (counter-momentum)", lambda t: t.spot_delta_5s < 0),
    ]:
        seg = [t for t in trades if fn(t)]
        print(f"  {label:50s} {_fmt_stats(_stats(seg))}")

    print("\n--- Segmentation: spot_delta_15s_bps ---")
    for label, fn in [
        ("delta15s == 0", lambda t: t.spot_delta_15s == 0.0),
        ("0 < delta15s <= 2.0", lambda t: 0 < t.spot_delta_15s <= 2.0),
        ("2.0 < delta15s <= 5.0", lambda t: 2.0 < t.spot_delta_15s <= 5.0),
        ("delta15s > 5.0", lambda t: t.spot_delta_15s > 5.0),
        ("delta15s < 0", lambda t: t.spot_delta_15s < 0),
    ]:
        seg = [t for t in trades if fn(t)]
        print(f"  {label:50s} {_fmt_stats(_stats(seg))}")

    # For continuation_soft specifically, find the best combined filter
    if setup == "continuation_soft":
        print("\n--- Combined filters (continuation_soft) ---")
        combos = [
            ("delta5s < 0  (counter-momentum)", lambda t: t.spot_delta_5s < 0),
            ("delta15s < 0  (counter-momentum 15s)", lambda t: t.spot_delta_15s < 0),
            ("delta5s < 0 AND delta15s < 0", lambda t: t.spot_delta_5s < 0 and t.spot_delta_15s < 0),
            ("delta5s < 0 AND secs >= 120", lambda t: t.spot_delta_5s < 0 and t.secs_to_end >= 120),
            ("delta5s < 0 AND secs >= 200", lambda t: t.spot_delta_5s < 0 and t.secs_to_end >= 200),
            ("delta5s > 0 AND delta15s > 0  (BLOCK candidate)", lambda t: t.spot_delta_5s > 0 and t.spot_delta_15s > 0),
            ("delta5s > 1  (moderate+ momentum)", lambda t: t.spot_delta_5s > 1.0),
        ]
        for label, fn in combos:
            seg = [t for t in trades if fn(t)]
            print(f"  {label:50s} {_fmt_stats(_stats(seg))}")


def _segment_reversal(trades: List[Trade], verbose: bool) -> None:
    """Segmentation for reversal setup."""
    print("\n--- Segmentation: entry_price ---")
    for label, fn in [
        ("entry < 0.05  (extreme low)", lambda t: t.entry_price < 0.05),
        ("entry 0.05-0.15", lambda t: 0.05 <= t.entry_price < 0.15),
        ("entry 0.15-0.30", lambda t: 0.15 <= t.entry_price < 0.30),
        ("entry >= 0.30", lambda t: t.entry_price >= 0.30),
    ]:
        seg = [t for t in trades if fn(t)]
        print(f"  {label:45s} {_fmt_stats(_stats(seg))}")

    print("\n--- Segmentation: spot_delta_5s_bps ---")
    for label, fn in [
        ("delta5s <= 1.0", lambda t: t.spot_delta_5s <= 1.0),
        ("1 < delta5s <= 3", lambda t: 1.0 < t.spot_delta_5s <= 3.0),
        ("delta5s > 3", lambda t: t.spot_delta_5s > 3.0),
    ]:
        seg = [t for t in trades if fn(t)]
        print(f"  {label:45s} {_fmt_stats(_stats(seg))}")


def _segment_trend_trailing(trades: List[Trade], verbose: bool) -> None:
    """Segmentation for current_trend_trailing."""
    print("\n--- Segmentation: entry_price ---")
    for label, fn in [
        ("entry < 0.60", lambda t: t.entry_price < 0.60),
        ("entry 0.60-0.80", lambda t: 0.60 <= t.entry_price < 0.80),
        ("entry >= 0.80", lambda t: t.entry_price >= 0.80),
    ]:
        seg = [t for t in trades if fn(t)]
        print(f"  {label:45s} {_fmt_stats(_stats(seg))}")

    print("\n--- Segmentation: secs_to_end ---")
    for label, fn in [
        ("secs < 60", lambda t: 0 <= t.secs_to_end < 60),
        ("secs 60-120", lambda t: 60 <= t.secs_to_end < 120),
        ("secs >= 120", lambda t: t.secs_to_end >= 120),
    ]:
        seg = [t for t in trades if fn(t)]
        print(f"  {label:45s} {_fmt_stats(_stats(seg))}")


def _segment_counter_reversal(trades: List[Trade], verbose: bool) -> None:
    """Segmentation for counter_reversal_near_resolved."""
    print("\n  (Very small sample — overall stats only.)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze entry quality across all research_base paper setups"
    )
    parser.add_argument("--setup", default=None, help="Filter to one setup name")
    parser.add_argument("--verbose", action="store_true", help="Extra detail")
    args = parser.parse_args()

    if not RESEARCH_BASE.exists():
        print(f"Research base not found: {RESEARCH_BASE}")
        return

    print(f"Reading {RESEARCH_BASE}...")
    trades = _parse_research_base(RESEARCH_BASE)

    # Group by setup
    by_setup: Dict[str, List[Trade]] = defaultdict(list)
    for t in trades:
        by_setup[t.setup].append(t)

    print(f"Total trades parsed: {len(trades)}")
    print(f"Setups found: {', '.join(f'{k}({len(v)})' for k, v in sorted(by_setup.items(), key=lambda x: -len(x[1])))}")

    setups_to_analyze = sorted(by_setup.keys(), key=lambda s: -len(by_setup[s]))
    if args.setup:
        setups_to_analyze = [s for s in setups_to_analyze if s == args.setup]
        if not setups_to_analyze:
            print(f"Setup '{args.setup}' not found. Available: {list(by_setup.keys())}")
            return

    for setup in setups_to_analyze:
        _analyze_setup(setup, by_setup[setup], args.verbose)

    print(f"\n{'='*70}")
    print("SUMMARY — setups with N>=20 and actionable insights:")
    print("=" * 70)
    for setup in setups_to_analyze:
        s = _stats(by_setup[setup])
        if s["n"] < 10:
            continue
        flag = ""
        if s["worst"] < -3:
            flag = "  *** CATASTROPHIC LOSS RISK ***"
        elif s["wr"] >= 90:
            flag = "  [high WR]"
        elif s["wr"] >= 70:
            flag = "  [moderate WR]"
        else:
            flag = "  [low WR - caution]"
        print(f"  {setup:35s} {_fmt_stats(s)}{flag}")


if __name__ == "__main__":
    main()
