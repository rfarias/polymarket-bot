"""
analyze_entry_quality_on_logs.py

Valida a hipótese de filtro por qualidade de entrada nos logs do runner real
(ou paper). Segmenta trades por exit_dist, setup_variant e range15s e mostra
win rate, PnL e pior perda por segmento.

Hipótese principal:
  exit_dist < 0.02 (1 tick do target) → 100% WR, zero losses no paper
  exit_dist >= 0.02 (standard)        → 88% WR, contém TODOS os losses

Uso no PC de casa (logs reais):
    python analyze_entry_quality_on_logs.py
    python analyze_entry_quality_on_logs.py --dir logs/current_almost_resolved_real_20260519_120000
    python analyze_entry_quality_on_logs.py --logs logs/session1/runner.jsonl

Uso com logs paper disponíveis neste PC (para referência):
    python analyze_entry_quality_on_logs.py --paper
    python analyze_entry_quality_on_logs.py --logs logs/current_almost_resolved_paper_resolved_pullback_safe_v2_3h_20260427_101148.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8")

EXIT_REAL = {"flat", "redeem_flat", "redeem_dust_archived", "external_close_detected"}
EXIT_PAPER = {"exit"}
EXIT_ALL = EXIT_REAL | EXIT_PAPER


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

def classify_tier(variant: str, exit_dist: float, entry: float, range15s: float) -> str:
    """
    Tier 1: exit_dist < 0.02  (1 tick do target — zero losses no paper)
    Tier 2: controlled_late_entry ou resolved_pullback_limit  (100% WR)
    Tier 3: standard + exit_dist >= 0.02 + range15s == 0 + entry >= 0.97
    Block:  todo o resto
    """
    if exit_dist < 0.02:
        return "T1_safe"
    if variant in ("controlled_late_entry", "resolved_pullback_limit"):
        return "T2_variant"
    if variant == "standard" and exit_dist < 0.04 and range15s == 0.0 and entry >= 0.97:
        return "T3_calm"
    return "BLOCK"


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

class TradeRecord:
    def __init__(self, slug: str, side: str, enter_ts: float,
                 entry_price: float, exit_dist: float, range15s: float,
                 variant: str, source_file: str):
        self.slug = slug
        self.side = side
        self.enter_ts = enter_ts
        self.entry_price = entry_price
        self.exit_dist = exit_dist
        self.range15s = range15s
        self.variant = variant
        self.source_file = source_file
        self.tier = classify_tier(variant, exit_dist, entry_price, range15s)
        # set after exit match
        self.exit_ts: Optional[float] = None
        self.exit_type: Optional[str] = None
        self.exit_reason: Optional[str] = None
        self.pnl_ticks: Optional[float] = None
        self.entry_qty_filled: float = 0.0

    @property
    def is_win(self) -> bool:
        return (self.pnl_ticks or 0.0) > 0


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _sf(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _parse_file(path: Path) -> List[TradeRecord]:
    records: List[TradeRecord] = []
    open_by_slug: Dict[str, TradeRecord] = {}
    open_by_ts: Dict[float, TradeRecord] = {}
    last_slug: str = ""

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
            ts = _sf(ev.get("ts"))

            if etype == "snapshot":
                s = str(ev.get("current_slug") or "")
                if s:
                    last_slug = s

            elif etype == "enter":
                # Skip non-current_almost_resolved entries in round-based paper logs
                if ev.get("setup") and ev.get("setup") != "current_almost_resolved":
                    continue

                sig = ev.get("signal") or {}
                tsnap = ev.get("trade") or {}
                side = str(sig.get("side") or tsnap.get("side") or "")
                slug = str(sig.get("event_slug") or tsnap.get("event_slug") or last_slug or "")
                entry_price = _sf(sig.get("entry_price") or tsnap.get("entry_price"))
                variant = str(sig.get("setup_variant") or tsnap.get("setup_variant") or "standard")
                range15s = _sf(sig.get("market_range_15s"))

                # exit_dist: distance from entry to target
                if side == "UP":
                    exit_dist = _sf(sig.get("up_exit_distance"))
                elif side == "DOWN":
                    exit_dist = _sf(sig.get("down_exit_distance"))
                else:
                    exit_dist = 0.0

                # Fallback: compute from entry_price and exit_price in signal
                if exit_dist == 0.0:
                    ep = _sf(sig.get("exit_price") or tsnap.get("target_price"))
                    if ep > 0 and entry_price > 0:
                        exit_dist = abs(ep - entry_price)

                if not slug:
                    continue

                # Close any prior open trade on same slug
                if slug in open_by_slug:
                    prev = open_by_slug.pop(slug)
                    prev.exit_type = "superceded"
                    records.append(prev)
                    open_by_ts.pop(prev.enter_ts, None)

                rec = TradeRecord(
                    slug=slug, side=side, enter_ts=ts,
                    entry_price=entry_price, exit_dist=exit_dist,
                    range15s=range15s, variant=variant,
                    source_file=path.name,
                )
                rec.entry_qty_filled = _sf(tsnap.get("entry_qty_requested") or 1.0)
                open_by_slug[slug] = rec
                open_by_ts[ts] = rec

            elif etype in EXIT_ALL:
                tsnap = ev.get("trade") or {}
                rec: Optional[TradeRecord] = None

                # Real runner: match by event_slug
                slug = str(tsnap.get("event_slug") or "")
                if slug and slug in open_by_slug:
                    rec = open_by_slug.pop(slug)
                    open_by_ts.pop(rec.enter_ts, None)

                # Paper: match by created_at == enter_ts
                if rec is None:
                    cat = _sf(tsnap.get("created_at"))
                    if cat and cat in open_by_ts:
                        rec = open_by_ts.pop(cat)
                        open_by_slug.pop(rec.slug, None)

                if rec is None:
                    continue

                rec.exit_ts = ts
                rec.exit_type = etype
                rec.exit_reason = str(tsnap.get("exit_reason") or "")

                # PnL: paper has direct pnl_ticks; real needs estimation
                direct_pnl = tsnap.get("pnl_ticks")
                if direct_pnl is not None:
                    rec.pnl_ticks = float(direct_pnl)
                else:
                    xp = _sf(tsnap.get("exit_price_posted") or tsnap.get("exit_price"))
                    ep = _sf(tsnap.get("entry_price") or rec.entry_price)
                    rec.entry_qty_filled = _sf(tsnap.get("entry_qty_filled"), rec.entry_qty_filled)
                    if ep > 0 and xp > 0:
                        rec.pnl_ticks = round((xp - ep) / 0.01, 1)

                records.append(rec)

    for rec in open_by_slug.values():
        rec.exit_type = "session_end_open"
        records.append(rec)

    return records


def _collect_files(args_logs: List[str], args_dir: Optional[str], paper_mode: bool) -> List[Path]:
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
        if paper_mode:
            # Paper logs known to have current_almost_resolved enter+exit events
            paper_candidates = [
                "current_almost_resolved_paper_resolved_pullback_safe_v2_3h_20260427_101148.jsonl",
                "current_almost_resolved_paper_resolved_pullback_safe_v2_1h.jsonl",
                "current_almost_resolved_paper_variant_1h.jsonl",
                "current_almost_resolved_paper_manual_eval_v1.jsonl",
                "current_almost_resolved_paper_resolved_pullback_safe_v1.jsonl",
                "current_almost_resolved_paper_resolved_pullback_safe_v3_20m.jsonl",
            ]
            for name in paper_candidates:
                p = base / name
                if p.exists():
                    files.append(p)
        else:
            # Real runner session directories
            for session_dir in sorted(base.glob("current_almost_resolved_real_*")):
                for jf in sorted(session_dir.glob("*.jsonl")):
                    files.append(jf)

    return [f for f in files if f.exists()]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

TIER_ORDER = ["T1_safe", "T2_variant", "T3_calm", "BLOCK"]
TIER_LABEL = {
    "T1_safe":    "Tier 1 — exit_dist<0.02  (1 tick do target)",
    "T2_variant": "Tier 2 — controlled_late / resolved_pullback",
    "T3_calm":    "Tier 3 — standard, calm market, entry>=0.97",
    "BLOCK":      "BLOCK   — standard, distante ou volátil",
}


def _wr(wins: int, total: int) -> str:
    if total == 0:
        return "N/A"
    return f"{wins/total*100:.0f}%"


def _print_report(trades: List[TradeRecord]) -> None:
    closed = [t for t in trades if t.exit_type in EXIT_ALL and t.pnl_ticks is not None]
    if not closed:
        print("\nNenhum trade completo encontrado nos logs.")
        return

    total = len(closed)
    wins_all = sum(1 for t in closed if t.is_win)

    print()
    print("=" * 70)
    print("ANÁLISE DE QUALIDADE DE ENTRADA")
    print(f"Total trades completos: {total}  |  WR geral: {_wr(wins_all, total)}")
    print("=" * 70)

    # By tier
    print()
    print(f"{'TIER':<52} {'N':>4}  {'WR':>5}  {'PnL':>6}  {'avg':>5}  {'pior':>5}")
    print("-" * 70)

    for tier in TIER_ORDER:
        sub = [t for t in closed if t.tier == tier]
        if not sub:
            continue
        n = len(sub)
        w = sum(1 for t in sub if t.is_win)
        pnl = sum(t.pnl_ticks for t in sub)
        avg = pnl / n
        worst = min(t.pnl_ticks for t in sub)
        label = TIER_LABEL[tier]
        print(f"  {label:<50} {n:>4}  {_wr(w, n):>5}  {pnl:>+6.0f}  {avg:>+5.2f}  {worst:>+5.1f}")

    # Allowed vs blocked summary
    allowed = [t for t in closed if t.tier != "BLOCK"]
    blocked = [t for t in closed if t.tier == "BLOCK"]
    print("-" * 70)
    n_a, w_a = len(allowed), sum(1 for t in allowed if t.is_win)
    n_b, w_b = len(blocked), sum(1 for t in blocked if t.is_win)
    pnl_a = sum(t.pnl_ticks for t in allowed)
    pnl_b = sum(t.pnl_ticks for t in blocked)
    worst_a = min((t.pnl_ticks for t in allowed), default=0)
    worst_b = min((t.pnl_ticks for t in blocked), default=0)
    print(f"  {'TOTAL PERMITIDO (T1+T2+T3)':<50} {n_a:>4}  {_wr(w_a, n_a):>5}  {pnl_a:>+6.0f}        {worst_a:>+5.1f}")
    print(f"  {'TOTAL BLOQUEADO':<50} {n_b:>4}  {_wr(w_b, n_b):>5}  {pnl_b:>+6.0f}        {worst_b:>+5.1f}")

    # Oportunidade perdida pelo bloqueio
    if n_b > 0:
        wins_lost = w_b
        losses_avoided = n_b - w_b
        print()
        print(f"  Custo do bloqueio    : {wins_lost} wins perdidos  ({_wr(wins_lost, n_b)} das bloqueadas)")
        print(f"  Benefício do bloqueio: {losses_avoided} losses evitados, pior={worst_b:+.1f}tk")

    # Exit distance distribution
    print()
    print("  Exit-dist distribution (trades allowed):")
    for label, fn in [
        ("exit_dist == 0.01 (1 tick)", lambda t: abs(t.exit_dist - 0.01) < 0.005),
        ("exit_dist == 0.02 (2 ticks)", lambda t: abs(t.exit_dist - 0.02) < 0.005),
        ("exit_dist 0.03–0.05",         lambda t: 0.025 <= t.exit_dist <= 0.055),
        ("exit_dist >= 0.06",           lambda t: t.exit_dist >= 0.055),
    ]:
        sub = [t for t in allowed if fn(t)]
        if not sub: continue
        n = len(sub); w = sum(1 for t in sub if t.is_win)
        worst = min(t.pnl_ticks for t in sub)
        print(f"    {label:<35} {n:>4}T  WR={_wr(w,n)}  worst={worst:+.1f}")

    # Worst losses detail
    losses_in_allowed = sorted([t for t in allowed if not t.is_win], key=lambda t: t.pnl_ticks)
    if losses_in_allowed:
        print()
        print(f"  Losses em trades PERMITIDOS ({len(losses_in_allowed)} total):")
        print(f"  {'slug':<45} {'side':<5} {'pnl':>5}  {'exit_dist':>9}  {'range15s':>8}  {'entry':>6}  {'variant'}")
        for t in losses_in_allowed[:15]:
            print(f"  {t.slug[:44]:<45} {t.side:<5} {t.pnl_ticks:>+5.1f}  {t.exit_dist:>9.3f}  {t.range15s:>8.3f}  {t.entry_price:>6.3f}  {t.variant}")

    losses_in_blocked = sorted([t for t in blocked if not t.is_win], key=lambda t: t.pnl_ticks)
    if losses_in_blocked:
        print()
        print(f"  Losses EVITADAS pelo bloqueio ({len(losses_in_blocked)} total):")
        print(f"  {'slug':<45} {'side':<5} {'pnl':>5}  {'exit_dist':>9}  {'range15s':>8}  {'entry':>6}  {'reason'}")
        for t in losses_in_blocked[:15]:
            print(f"  {t.slug[:44]:<45} {t.side:<5} {t.pnl_ticks:>+5.1f}  {t.exit_dist:>9.3f}  {t.range15s:>8.3f}  {t.entry_price:>6.3f}  {t.exit_reason or '?'}")

    # Per-variant detail
    print()
    print("  Por setup_variant:")
    variants: Dict[str, List[TradeRecord]] = defaultdict(list)
    for t in closed:
        variants[t.variant].append(t)
    for v in sorted(variants.keys()):
        sub = variants[v]
        n = len(sub); w = sum(1 for t in sub if t.is_win)
        pnl = sum(t.pnl_ticks for t in sub)
        worst = min(t.pnl_ticks for t in sub)
        print(f"    {v:<45} {n:>4}T  WR={_wr(w,n)}  pnl={pnl:+.0f}  worst={worst:+.1f}")

    print("=" * 70)

    # Quick check: are exit_dist fields populated?
    missing_dist = sum(1 for t in closed if t.exit_dist == 0.0)
    if missing_dist > len(closed) * 0.5:
        print()
        print(f"  AVISO: {missing_dist}/{len(closed)} trades com exit_dist=0.")
        print("  Verifique se o campo up_exit_distance/down_exit_distance está no sinal.")
        print("  Isso pode acontecer em logs de versões antigas do runner.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Valida filtros de qualidade de entrada nos logs do runner"
    )
    parser.add_argument("--logs", nargs="+", default=[], help="Arquivo(s) JSONL específico(s)")
    parser.add_argument("--dir", default=None, help="Diretório com arquivos JSONL")
    parser.add_argument(
        "--paper", action="store_true",
        help="Usar logs paper disponíveis neste PC (para referência/desenvolvimento)"
    )
    args = parser.parse_args()

    files = _collect_files(args.logs, args.dir, paper_mode=args.paper)
    if not files:
        print("Nenhum arquivo JSONL encontrado.")
        print()
        print("Uso:")
        print("  PC de casa (logs reais):  python analyze_entry_quality_on_logs.py")
        print("  PC de dev (logs paper):   python analyze_entry_quality_on_logs.py --paper")
        print("  Diretório específico:     python analyze_entry_quality_on_logs.py --dir logs/minha_sessao")
        return

    print(f"Encontrados {len(files)} arquivo(s):")
    for f in files[:10]:
        print(f"  {f}")
    if len(files) > 10:
        print(f"  ... +{len(files)-10} arquivos")

    all_trades: List[TradeRecord] = []
    for f in files:
        batch = _parse_file(f)
        closed = [t for t in batch if t.exit_type in EXIT_ALL and t.pnl_ticks is not None]
        print(f"  {f.name}: {len(batch)} registros, {len(closed)} trades completos")
        all_trades.extend(batch)

    _print_report(all_trades)


if __name__ == "__main__":
    main()
