"""Resumo completo: acumulado, por sessão e el_vel vs outcome.

Modos:
  python _check_results_full.py          # EE paper (ee_paper_*)
  python _check_results_full.py --ar     # AR runner real (current_almost_resolved_real_*)
"""
import json
import argparse
from pathlib import Path
from collections import Counter, defaultdict

parser = argparse.ArgumentParser()
parser.add_argument("--ar", action="store_true", help="Usar logs do runner AR real")
args = parser.parse_args()

QTY = 6.0


def _ar_outcome(last_reason: str, source: str, exit_price, entry_price) -> str:
    r = (last_reason or "").lower()
    s = (source or "").lower()

    if "stop" in r:
        return "STOP_LOSS"
    if "mid_book_reversal" in r or "reversal" in r:
        return "REVERSAL"
    if "ar_hedge" in r or "win_hedge" in r:
        return "WIN_HEDGE"
    if "profit_protect" in r:
        return "PROFIT_PROTECT"
    if "near_win_exit" in r or "oracle_win_exit" in r or "oracle_margin_exit" in r:
        return "WIN"
    if "win" in r:
        return "WIN"
    if s in ("redeem_flat", "startup_reconcile"):
        if exit_price is not None and exit_price >= 0.99:
            return "WIN"
        if exit_price is not None and exit_price <= 0.01:
            return "LOSS"
    # Fallback por preço
    if exit_price is not None and entry_price:
        return "WIN" if exit_price > entry_price else "STOP_LOSS"
    return "UNKNOWN"


def _load_ar_session(lp: Path) -> list:
    """Lê uma sessão AR real e retorna lista de trades, sem duplicatas."""
    rows = []
    try:
        rows = [json.loads(l) for l in lp.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except Exception:
        return []

    session = lp.parent.name
    by_slug: dict = {}

    # 1. Preferir trade_closed (formato unificado, mais recente)
    for c in (r for r in rows if r.get("type") == "trade_closed"):
        ep  = c.get("entry_price") or 0.0
        xp  = c.get("exit_price")
        qty = c.get("qty") or QTY
        pnl = c.get("pnl_usd")
        rsn = c.get("last_reason") or ""
        src = c.get("source") or ""
        side = c.get("side") or ""
        slug = c.get("entry_slug") or ""

        if pnl is None and xp is not None and ep:
            pnl = round((xp - ep) * qty, 4)
        if pnl is None or not ep:
            continue

        outcome = _ar_outcome(rsn, src, xp, ep)
        key = slug or str(c.get("ts", ""))
        by_slug[key] = {
            "slug": slug[-6:], "ep": ep, "side": side,
            "el_vel": 0.0, "outcome": outcome, "pnl": pnl,
            "session": session, "ts": c.get("ts", 0), "last_reason": rsn,
        }

    # 2. Fallback: flat (trade embedded)
    for e in (r for r in rows if r.get("type") == "flat"):
        tr  = e.get("trade") or {}
        ep  = tr.get("entry_price") or 0.0
        xp  = tr.get("exit_price_posted")
        eq  = tr.get("entry_qty_filled") or 0.0
        xq  = tr.get("exit_qty_filled") or 0.0
        rsn = tr.get("last_reason") or ""
        side = tr.get("side") or ""
        slug = tr.get("event_slug") or ""

        if eq <= 0 or xq <= 0 or not ep:
            continue

        qty = xq
        pnl = round((xp - ep) * qty, 4) if xp is not None else None
        if pnl is None:
            continue

        outcome = _ar_outcome(rsn, "", xp, ep)
        key = slug or str(e.get("ts", ""))
        if key not in by_slug:
            by_slug[key] = {
                "slug": slug[-6:], "ep": ep, "side": side,
                "el_vel": 0.0, "outcome": outcome, "pnl": pnl,
                "session": session, "ts": e.get("ts", 0), "last_reason": rsn,
            }

    # 3. Fallback: redeem_flat
    for e in (r for r in rows if r.get("type") == "redeem_flat"):
        tr   = e.get("trade") or {}
        ep   = tr.get("entry_price") or 0.0
        xp   = e.get("exit_price")
        pnl  = e.get("pnl_usd")
        rsn  = tr.get("last_reason") or ""
        side = tr.get("side") or ""
        slug = tr.get("event_slug") or ""
        qty  = tr.get("entry_qty_filled") or QTY

        if not ep or pnl is None:
            continue

        outcome = _ar_outcome(rsn, "redeem_flat", xp, ep)
        key = slug or str(e.get("ts", ""))
        if key not in by_slug:
            by_slug[key] = {
                "slug": slug[-6:], "ep": ep, "side": side,
                "el_vel": 0.0, "outcome": outcome, "pnl": pnl,
                "session": session, "ts": e.get("ts", 0), "last_reason": rsn,
            }

    return list(by_slug.values())


# ── Carregar trades ────────────────────────────────────────────────────────────
trades = []

if args.ar:
    sessions = sorted(Path("logs").glob("current_almost_resolved_real_*/current_almost_resolved_real.jsonl"))
    for lp in sessions:
        trades.extend(_load_ar_session(lp))

else:
    sessions = sorted(Path("logs").glob("ee_paper_*/ee_paper.jsonl"))
    for lp in sessions:
        rows = []
        try:
            rows = [json.loads(l) for l in lp.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        except Exception:
            continue

        startup = next((r for r in rows if r.get("type") == "startup"), None)
        ts_start = startup.get("ts", 0) if startup else 0
        entries = {r["slug"]: r for r in rows if r.get("type") == "ee_paper_entry"}
        closed  = {r["slug"]: r for r in rows if r.get("type") == "ee_paper_closed"}

        for slug, e in entries.items():
            cl = closed.get(slug)
            outcome = cl["ee"]["outcome"] if cl else "aberta"
            pnl     = cl["ee"]["pnl"]    if cl else None
            trades.append({
                "slug": slug[-6:], "ep": e.get("ep", 0), "side": "",
                "el_vel": e.get("el", {}).get("el_vel", 0),
                "outcome": outcome, "pnl": pnl,
                "session": lp.parent.name, "ts": e.get("ts", ts_start),
                "last_reason": "",
            })


closed_t = [t for t in trades if t["outcome"] not in ("aberta", "MISSED")]
mode_label = "AR runner real" if args.ar else "EE paper"

# ── Acumulado geral ────────────────────────────────────────────────────────────
c = Counter(t["outcome"] for t in closed_t)
pnl_tot = round(sum(t["pnl"] for t in closed_t), 2)
n = len(closed_t)
if n == 0:
    print("Nenhum trade fechado encontrado.")
    raise SystemExit(0)

lucro = sum(1 for t in closed_t if t["pnl"] > 0)

print(f"\n{'='*60}")
print(f"  ACUMULADO TOTAL [{mode_label}] — {n} trades fechados")
print(f"{'='*60}")
print(f"  WIN            : {c.get('WIN', 0)}")
print(f"  PROFIT_PROTECT : {c.get('PROFIT_PROTECT', 0)}")
print(f"  WIN_HEDGE      : {c.get('WIN_HEDGE', 0)}")
print(f"  STOP_LOSS      : {c.get('STOP_LOSS', 0)}")
print(f"  REVERSAL       : {c.get('REVERSAL', 0)}")
print(f"  LOSS           : {c.get('LOSS', 0)}")
outros = {k: v for k, v in c.items() if k not in ("WIN", "PROFIT_PROTECT", "WIN_HEDGE", "STOP_LOSS", "REVERSAL", "LOSS")}
if outros:
    for k, v in sorted(outros.items()):
        print(f"  {k:<15}: {v}")
print(f"  WR (lucro>0)   : {lucro}/{n} = {lucro/n:.1%}")
print(f"  PnL total      : {pnl_tot:>+.2f}")
print(f"  avg / trade    : {pnl_tot/n:>+.3f}")

# ── Breakdown por sessão (últimas com trades) ─────────────────────────────────
by_session = defaultdict(list)
for t in closed_t:
    by_session[t["session"]].append(t)

if len(by_session) > 1:
    sessions_com_trades = sorted(by_session.keys())
    print(f"\n  Sessões com trades ({len(sessions_com_trades)} no total):")
    for sess in sessions_com_trades[-15:]:
        grp = by_session[sess]
        sp  = round(sum(t["pnl"] for t in grp), 2)
        sw  = sum(1 for t in grp if t["pnl"] > 0)
        sc  = Counter(t["outcome"] for t in grp)
        print(f"    {sess[-19:]}  n={len(grp):>2}  WR={sw/len(grp):.0%}  PnL={sp:>+6.2f}  "
              f"W={sc.get('WIN',0)} PP={sc.get('PROFIT_PROTECT',0)} SL={sc.get('STOP_LOSS',0)}")

# ── el_vel por outcome (só EE paper) ─────────────────────────────────────────
if not args.ar:
    print(f"\n{'='*60}")
    print(f"  el_vel POR OUTCOME")
    print(f"{'='*60}")
    by_out = defaultdict(list)
    for t in closed_t:
        by_out[t["outcome"]].append(t["el_vel"])
    for out in ["WIN", "PROFIT_PROTECT", "STOP_LOSS", "WIN_HEDGE", "REVERSAL"]:
        vels = by_out.get(out, [])
        if not vels:
            continue
        print(f"  {out:<16}: n={len(vels):>3}  avg={sum(vels)/len(vels):.3f}  "
              f"min={min(vels):.3f}  max={max(vels):.3f}")

    print(f"\n{'='*60}")
    print(f"  SIMULAÇÃO GATE el_vel ({n} trades)")
    print(f"{'='*60}")
    print(f"  {'Gate':>8}  {'n':>5}  {'WR':>7}  {'STOP':>6}  {'STOP%':>7}  {'PnL':>9}  {'avg':>7}")
    print("  " + "-"*55)
    for gate in [0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.15]:
        sub = [t for t in closed_t if t["el_vel"] >= gate]
        if not sub:
            continue
        nl = sum(1 for t in sub if t["pnl"] > 0)
        ns = sum(1 for t in sub if t["outcome"] == "STOP_LOSS")
        pnl = round(sum(t["pnl"] for t in sub), 2)
        blk = n - len(sub)
        print(f"  {gate:>8.2f}  {len(sub):>5}  {nl/len(sub):>7.1%}  {ns:>6}  "
              f"{ns/len(sub):>7.1%}  {pnl:>+9.2f}  {pnl/len(sub):>+7.3f}  (bloqueia {blk})")
    print(f"  {'base':>8}  {n:>5}  {lucro/n:>7.1%}  {c.get('STOP_LOSS',0):>6}  "
          f"{c.get('STOP_LOSS',0)/n:>7.1%}  {pnl_tot:>+9.2f}  {pnl_tot/n:>+7.3f}")

# ── AR: distribuição de last_reason ──────────────────────────────────────────
if args.ar:
    print(f"\n{'='*60}")
    print(f"  DISTRIBUIÇÃO POR last_reason")
    print(f"{'='*60}")
    reason_map = defaultdict(list)
    for t in closed_t:
        r = t["last_reason"] or "(vazio)"
        parts = r.split(":")
        key = ":".join(parts[:3]) if len(parts) >= 3 else r
        reason_map[key].append(t["pnl"])
    for key in sorted(reason_map, key=lambda k: -len(reason_map[k])):
        grp = reason_map[key]
        gp  = round(sum(grp), 2)
        gw  = sum(1 for p in grp if p > 0)
        print(f"  {key:<50}  n={len(grp):>3}  WR={gw/len(grp):.0%}  PnL={gp:>+7.2f}")

# ── Stops: distribuição de gaps ───────────────────────────────────────────────
stops = [t for t in closed_t if t["outcome"] == "STOP_LOSS"]
gaps = []
if stops:
    if args.ar:
        # gap = quanto o preço caiu além do stop esperado
        gaps = [round(t["ep"] - (t["ep"] + t["pnl"] / QTY), 3) for t in stops if t["pnl"] < 0]
    else:
        gaps = [round(0.65 - (t["ep"] + t["pnl"] / QTY), 3) for t in stops]

print(f"\n  STOP_LOSS: {len(stops)} trades")
if gaps:
    print(f"    avg_exit_gap={sum(gaps)/len(gaps):.3f}  min={min(gaps):.3f}  max={max(gaps):.3f}")
    buckets = [(0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.50)]
    for lo, hi in buckets:
        nb = sum(1 for g in gaps if lo <= g < hi)
        if nb:
            print(f"    gap [{lo:.2f}-{hi:.2f}): {nb} trades")
else:
    print(f"    n=0  (sem stops com dados de saída)")
