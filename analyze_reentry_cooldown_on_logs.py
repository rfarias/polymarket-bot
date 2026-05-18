"""
analyze_reentry_cooldown_on_logs.py

Simulates the 25s re-entry cooldown on existing runner JSONL logs.

For each log session it:
  1. Pairs enter -> flat/redeem_flat events by slug
  2. Detects consecutive trades on the same slug where
     enter_ts[i+1] - exit_ts[i] < cooldown_secs
  3. Reports which entries would have been blocked and their PnL

Usage:
    python analyze_reentry_cooldown_on_logs.py
    python analyze_reentry_cooldown_on_logs.py --dir logs
    python analyze_reentry_cooldown_on_logs.py --logs logs/session1/runner.jsonl
    python analyze_reentry_cooldown_on_logs.py --cooldown 25
    python analyze_reentry_cooldown_on_logs.py --check-existing
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8")

COOLDOWN_SECS_DEFAULT = 25.0

EXIT_EVENT_TYPES = {
    "flat",
    "redeem_flat",
    "redeem_dust_archived",
    "external_close_detected",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class TradeRecord:
    def __init__(self, slug: str, side: str, enter_ts: float, entry_price: float,
                 setup_variant: str, source_file: str):
        self.slug = slug
        self.side = side
        self.enter_ts = enter_ts
        self.entry_price = entry_price
        self.setup_variant = setup_variant
        self.source_file = source_file
        self.exit_ts: Optional[float] = None
        self.exit_type: Optional[str] = None
        self.exit_price_posted: Optional[float] = None
        self.entry_qty_filled: float = 0.0
        self.pnl_ticks_est: Optional[float] = None

    @property
    def is_closed(self) -> bool:
        return self.exit_ts is not None

    @property
    def hold_secs(self) -> Optional[float]:
        if self.exit_ts and self.enter_ts:
            return round(self.exit_ts - self.enter_ts, 1)
        return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _parse_trades_from_file(path: Path) -> List[TradeRecord]:
    """
    Parse enter/exit pairs from a single runner JSONL file.
    Matches by event_slug (FIFO). Unmatched enters are reported as open/unknown.
    """
    records: List[TradeRecord] = []
    open_by_slug: Dict[str, TradeRecord] = {}  # slug -> open trade

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
            ts = _safe_float(ev.get("ts"), 0.0)

            if etype == "enter":
                sig = ev.get("signal") or {}
                trade_snap = ev.get("trade") or {}
                slug = str(sig.get("event_slug") or trade_snap.get("event_slug") or "")
                side = str(sig.get("side") or trade_snap.get("side") or "")
                entry_price = _safe_float(sig.get("entry_price") or trade_snap.get("entry_price"), 0.0)
                variant = str(sig.get("setup_variant") or trade_snap.get("setup_variant") or "unknown")
                qty = _safe_float(trade_snap.get("entry_qty_requested"), 0.0)

                if not slug:
                    continue

                # If there's already an open trade for this slug, close it as unknown
                if slug in open_by_slug:
                    prev = open_by_slug.pop(slug)
                    prev.exit_ts = ts
                    prev.exit_type = "superceded_by_enter"
                    records.append(prev)

                rec = TradeRecord(
                    slug=slug, side=side, enter_ts=ts,
                    entry_price=entry_price, setup_variant=variant,
                    source_file=path.name,
                )
                rec.entry_qty_filled = qty
                open_by_slug[slug] = rec

            elif etype in EXIT_EVENT_TYPES:
                trade_snap = ev.get("trade") or {}
                slug = str(trade_snap.get("event_slug") or "")
                if not slug:
                    continue

                rec = open_by_slug.pop(slug, None)
                if rec is None:
                    # exit without matching enter (session restored from state)
                    continue

                rec.exit_ts = ts
                rec.exit_type = etype
                rec.exit_price_posted = _safe_float(trade_snap.get("exit_price_posted"), 0.0)
                rec.entry_qty_filled = _safe_float(trade_snap.get("entry_qty_filled"), rec.entry_qty_filled)

                # Estimate PnL in ticks (tick size ~0.01 for most markets)
                ep = _safe_float(trade_snap.get("entry_price"), rec.entry_price)
                xp = rec.exit_price_posted
                tick = 0.01
                if ep > 0 and xp and xp > 0:
                    rec.pnl_ticks_est = round((xp - ep) / tick, 1)

                records.append(rec)

    # Any remaining open trades — session ended while position was open
    for rec in open_by_slug.values():
        rec.exit_type = "session_end_open"
        records.append(rec)

    return records


def _collect_files(args_logs: List[str], args_dir: Optional[str]) -> List[Path]:
    files: List[Path] = []
    for lp in (args_logs or []):
        p = Path(lp)
        if p.exists():
            files.append(p)

    if args_dir:
        d = Path(args_dir)
        files.extend(sorted(d.glob("**/*.jsonl")))

    if not files:
        base = Path("logs")
        # Auto-detect runner session directories
        for session_dir in sorted(base.glob("current_almost_resolved_real_*")):
            for jf in sorted(session_dir.glob("*.jsonl")):
                files.append(jf)

    return [f for f in files if f.exists()]


# ---------------------------------------------------------------------------
# Cooldown simulation
# ---------------------------------------------------------------------------

def _simulate_cooldown(
    trades: List[TradeRecord],
    cooldown_secs: float,
) -> Tuple[List[TradeRecord], List[Tuple[TradeRecord, TradeRecord, float]]]:
    """
    Returns:
        passed  — trades that would NOT have been blocked
        blocked — list of (blocked_trade, prior_trade, gap_secs) tuples
    """
    # Only real exits (flat, redeem_flat, etc.) — not session_end_open or superceded
    closed = [t for t in trades if t.is_closed and t.exit_type in EXIT_EVENT_TYPES]
    closed.sort(key=lambda t: t.enter_ts)

    # last exit time per slug
    last_exit_ts: Dict[str, float] = {}
    last_trade: Dict[str, TradeRecord] = {}

    passed: List[TradeRecord] = []
    blocked: List[Tuple[TradeRecord, TradeRecord, float]] = []

    for t in closed:
        prior_exit = last_exit_ts.get(t.slug, 0.0)
        gap = t.enter_ts - prior_exit
        if prior_exit > 0 and gap < cooldown_secs:
            blocked.append((t, last_trade[t.slug], round(gap, 1)))
        else:
            passed.append(t)

        if t.exit_ts:
            last_exit_ts[t.slug] = t.exit_ts
            last_trade[t.slug] = t

    return passed, blocked


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _fmt_ts(ts: float) -> str:
    import time
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return f"{ts:.0f}"


def _print_report(
    all_trades: List[TradeRecord],
    passed: List[TradeRecord],
    blocked: List[Tuple[TradeRecord, TradeRecord, float]],
    cooldown_secs: float,
) -> None:
    closed = [t for t in all_trades if t.is_closed and t.exit_type in EXIT_EVENT_TYPES]

    total = len(closed)
    n_blocked = len(blocked)
    n_passed = len(passed)

    wins_all = sum(1 for t in closed if t.pnl_ticks_est is not None and t.pnl_ticks_est > 0)
    wins_passed = sum(1 for t in passed if t.pnl_ticks_est is not None and t.pnl_ticks_est > 0)
    losses_all = sum(1 for t in closed if t.pnl_ticks_est is not None and t.pnl_ticks_est <= 0)

    def wr(w: int, t: int) -> str:
        return f"{w/t*100:.1f}%  ({w}W/{t-w}L/{t}T)" if t > 0 else "N/A"

    print()
    print("=" * 70)
    print(f"RE-ENTRY COOLDOWN SIMULATION  (cooldown={cooldown_secs:.0f}s)")
    print("=" * 70)
    print(f"  Total completed trades : {total}")
    print(f"  Would pass cooldown    : {n_passed}")
    pct_blocked = f"{n_blocked/total*100:.1f}%" if total > 0 else "N/A"
    print(f"  Would be BLOCKED       : {n_blocked}  ({pct_blocked} of trades)")
    print()

    if total > 0:
        wins_blocked = sum(1 for t, _, _ in blocked if t.pnl_ticks_est is not None and t.pnl_ticks_est > 0)
        losses_blocked = sum(1 for t, _, _ in blocked if t.pnl_ticks_est is not None and t.pnl_ticks_est <= 0)
        print(f"  Win rate ALL trades    : {wr(wins_all, total)}")
        print(f"  Win rate PASSED trades : {wr(wins_passed, n_passed)}")
        if n_blocked > 0:
            print(f"  Blocked breakdown      : {wins_blocked}W / {losses_blocked}L  (pnl_ticks estimated)")

    print()
    if not blocked:
        print("  No re-entry patterns found — cooldown would not have affected any trades.")
        print("=" * 70)
        return

    pnl_sum = sum(t.pnl_ticks_est for t, _, _ in blocked if t.pnl_ticks_est is not None)
    print(f"  Estimated ticks SAVED by blocking : {-pnl_sum:+.1f}  (negative pnl avoided)")
    print()
    print("  BLOCKED ENTRIES DETAIL:")
    print(f"  {'enter_ts':<22} {'slug':<45} {'side':<5} {'gap':>5}s  {'pnl_est':>8}  {'exit_type'}")
    print("  " + "-" * 105)
    for t, prior, gap in sorted(blocked, key=lambda x: x[0].enter_ts):
        pnl_str = f"{t.pnl_ticks_est:+.1f}" if t.pnl_ticks_est is not None else "   ?"
        print(
            f"  {_fmt_ts(t.enter_ts):<22} {t.slug[:44]:<45} {t.side:<5} {gap:>5.1f}s"
            f"  {pnl_str:>8}  {t.exit_type}"
        )
        print(
            f"    prior exit: {_fmt_ts(prior.exit_ts or 0):<22}  entry_price={t.entry_price:.4f}  "
            f"exit_price_posted={t.exit_price_posted or '?'}"
        )

    print()
    print("  Re-entry gap distribution (all blocked):")
    gaps = [g for _, _, g in blocked]
    for threshold in [5, 10, 15, 20, 25]:
        count = sum(1 for g in gaps if g < threshold)
        print(f"    gap < {threshold:2d}s : {count}")

    print("=" * 70)


def _check_existing_blocks(files: List[Path]) -> None:
    """Look for reentry_cooldown blocks already present in logs (after fix deployed)."""
    print()
    print("=" * 70)
    print("EXISTING reentry_cooldown BLOCKS IN LOGS")
    print("=" * 70)

    total_blocks = 0
    for f in files:
        session_blocks: List[dict] = []
        try:
            with open(f, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "entry_blocked" and ev.get("reason") == "reentry_cooldown":
                        session_blocks.append(ev)
        except Exception:
            continue

        if session_blocks:
            print(f"\n  {f}  ({len(session_blocks)} blocks)")
            for ev in session_blocks[:20]:
                sig = ev.get("signal") or {}
                slug = sig.get("event_slug") or "?"
                side = sig.get("side") or "?"
                remaining = ev.get("cooldown_remaining_secs") or "?"
                print(f"    {_fmt_ts(_safe_float(ev.get('ts')))}  slug={slug[:40]}  side={side}  remaining={remaining}s")
            total_blocks += len(session_blocks)

    if total_blocks == 0:
        print("  None found — cooldown fix not yet deployed on these logs, or no re-entries occurred.")
    else:
        print(f"\n  Total reentry_cooldown blocks: {total_blocks}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate 25s re-entry cooldown on existing runner logs"
    )
    parser.add_argument("--logs", nargs="+", default=[], help="Specific JSONL file(s)")
    parser.add_argument("--dir", default=None, help="Directory to scan for JSONL files")
    parser.add_argument(
        "--cooldown", type=float, default=COOLDOWN_SECS_DEFAULT,
        help=f"Cooldown seconds to simulate (default {COOLDOWN_SECS_DEFAULT})"
    )
    parser.add_argument(
        "--check-existing", action="store_true",
        help="Also scan for reentry_cooldown blocks already present in logs (post-fix)"
    )
    args = parser.parse_args()

    files = _collect_files(args.logs, args.dir)
    if not files:
        print("No JSONL files found. Use --logs, --dir, or place logs in logs/current_almost_resolved_real_*/")
        return

    print(f"Found {len(files)} JSONL file(s):")
    for f in files:
        print(f"  {f}")

    all_trades: List[TradeRecord] = []
    for f in files:
        batch = _parse_trades_from_file(f)
        closed = [t for t in batch if t.is_closed and t.entry_qty_filled > 0]
        print(f"  {f.name}: {len(batch)} total records, {len(closed)} completed with fill")
        all_trades.extend(batch)

    if not all_trades:
        print("No trades found.")
        return

    passed, blocked = _simulate_cooldown(all_trades, cooldown_secs=args.cooldown)
    _print_report(all_trades, passed, blocked, args.cooldown)

    if args.check_existing:
        _check_existing_blocks(files)


if __name__ == "__main__":
    main()
