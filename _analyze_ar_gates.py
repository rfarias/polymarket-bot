"""
Simula gates de volatilidade e distancia nos logs do runner real AR.

Cruza: evento `enter` (campos de volatilidade/distancia) com `trade_closed` (PnL confiável).
Usa APENAS trade_closed para garantir PnL correto — flat events têm exit_price_posted
que pode ser 0.0 para posições resolvidas pela plataforma, distorcendo o cálculo.

Gates analisados:
  1. market_range_15s  — volatilidade de curto prazo antes da entrada
  2. market_range_30s  — volatilidade de médio prazo
  3. distance_to_price_to_beat_bps — margem de segurança vs ponto de equilíbrio
  4. passive_capture_safe_distance_ok — flag do runner
"""
import json
import sys
import io
from pathlib import Path
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

QTY = 6.0
sessions = sorted(Path("logs").glob("current_almost_resolved_real_*/current_almost_resolved_real.jsonl"))

records = []

for lp in sessions:
    try:
        rows = [json.loads(l) for l in lp.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except Exception:
        continue

    # Indexar enters por slug (guardar campos de volatilidade do signal)
    enters_by_slug: dict = {}
    for r in rows:
        if r.get("type") == "enter":
            sig  = r.get("signal") or {}
            slug = sig.get("event_slug") or (r.get("trade") or {}).get("event_slug") or ""
            if slug and "distance_to_price_to_beat_bps" in sig:
                enters_by_slug[slug] = sig

    if not enters_by_slug:
        continue  # sessão sem campos de volatilidade

    # Coletar trade_closed (PnL confiável)
    for c in (r for r in rows if r.get("type") == "trade_closed"):
        slug = c.get("entry_slug") or ""
        if slug not in enters_by_slug:
            continue

        ep   = c.get("entry_price") or 0.0
        xp   = c.get("exit_price")
        qty  = c.get("qty") or QTY
        pnl  = c.get("pnl_usd")
        rsn  = c.get("last_reason") or ""
        src  = c.get("source") or ""

        if pnl is None and xp is not None and ep:
            pnl = round((xp - ep) * qty, 4)
        if pnl is None or not ep:
            continue

        sig   = enters_by_slug[slug]
        r15   = sig.get("market_range_15s")
        r30   = sig.get("market_range_30s") or 0.0
        r60   = sig.get("market_range_60s") or 0.0
        d_bps = sig.get("distance_to_price_to_beat_bps")
        d_usd = sig.get("distance_to_price_to_beat_usd") or 0.0
        safe  = sig.get("passive_capture_safe_distance_ok")
        side  = c.get("side") or ""

        if r15 is None or d_bps is None:
            continue

        records.append({
            "slug":  slug[-14:],
            "pnl":   pnl,
            "ep":    ep,
            "xp":    xp,
            "rsn":   rsn,
            "side":  side,
            "r15":   r15,
            "r30":   r30,
            "r60":   r60,
            "d_bps": d_bps,
            "d_usd": d_usd,
            "safe":  safe,
            "win":   pnl > 0,
        })

n = len(records)
if n == 0:
    print("Nenhum trade_closed com campos de volatilidade encontrado.")
    print("(Sessões anteriores a mai/22 podem não ter esses campos)")
    raise SystemExit(0)

wins  = [r for r in records if r["win"]]
stops = [r for r in records if not r["win"]]
pnl_t = round(sum(r["pnl"] for r in records), 2)

print(f"\n{'='*65}")
print(f"  ANÁLISE DE GATES — AR runner real  ({n} trades)")
print(f"{'='*65}")
print(f"  Total: {n}  WIN={len(wins)}  LOSS={len(stops)}  WR={len(wins)/n:.1%}  PnL={pnl_t:+.2f}")


def _stats(vals):
    if not vals:
        return "n=0"
    srt = sorted(vals)
    avg = sum(vals) / len(vals)
    p25 = srt[max(0, int(len(srt)*0.25))]
    p75 = srt[min(len(srt)-1, int(len(srt)*0.75))]
    p90 = srt[min(len(srt)-1, int(len(srt)*0.90))]
    return f"avg={avg:.3f}  p25={p25:.3f}  p75={p75:.3f}  p90={p90:.3f}  min={srt[0]:.3f}  max={srt[-1]:.3f}"


# ── Distribuição WIN vs LOSS ─────────────────────────────────────────────────
print(f"\n  {'Campo':<30}  {'WIN':>45}  {'LOSS':>45}")
print("  " + "─"*125)
for field, label in [("r15","range_15s"), ("r30","range_30s"), ("d_bps","dist_ptb_bps")]:
    wv = [r[field] for r in wins]
    lv = [r[field] for r in stops]
    print(f"  {label:<30}  {_stats(wv):>45}  {_stats(lv):>45}")


# ── Gate range_15s ───────────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  GATE: bloquear entrada quando range_15s >= X  (mercado agitado)")
print(f"{'='*65}")
print(f"  {'Gate':>8}  {'n':>4}  {'WR':>6}  {'PnL':>8}  {'avg':>7}  {'bloq':>5}  {'bloq%':>6}  bloq_PnL")
print("  " + "─"*60)
for thr in [0.01, 0.02, 0.03, 0.04, 0.05]:
    sub = [r for r in records if r["r15"] < thr]
    if not sub:
        continue
    sw = sum(1 for r in sub if r["win"])
    sp = round(sum(r["pnl"] for r in sub), 2)
    blk = n - len(sub)
    blk_pnl = round(sum(r["pnl"] for r in records if r["r15"] >= thr), 2)
    print(f"  {f'<{thr:.2f}':>8}  {len(sub):>4}  {sw/len(sub):>6.1%}  {sp:>+8.2f}  {sp/len(sub):>+7.3f}  "
          f"{blk:>5}  {blk/n:>6.1%}  {blk_pnl:+.2f}")
print(f"  {'base':>8}  {n:>4}  {len(wins)/n:>6.1%}  {pnl_t:>+8.2f}  {pnl_t/n:>+7.3f}")


# ── Gate distance_to_ptb ─────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  GATE: bloquear entrada quando distance_to_ptb_bps < X  (margem pequena)")
print(f"{'='*65}")
print(f"  {'Gate':>10}  {'n':>4}  {'WR':>6}  {'PnL':>8}  {'avg':>7}  {'bloq':>5}  {'bloq%':>6}  bloq_PnL")
print("  " + "─"*62)
for thr in [0, 5, 8, 10, 12, 15]:
    sub = [r for r in records if r["d_bps"] >= thr]
    if not sub:
        continue
    sw = sum(1 for r in sub if r["win"])
    sp = round(sum(r["pnl"] for r in sub), 2)
    blk = n - len(sub)
    blk_pnl = round(sum(r["pnl"] for r in records if r["d_bps"] < thr), 2)
    print(f"  {f'>={thr}bps':>10}  {len(sub):>4}  {sw/len(sub):>6.1%}  {sp:>+8.2f}  {sp/len(sub):>+7.3f}  "
          f"{blk:>5}  {blk/n:>6.1%}  {blk_pnl:+.2f}")
print(f"  {'base':>10}  {n:>4}  {len(wins)/n:>6.1%}  {pnl_t:>+8.2f}  {pnl_t/n:>+7.3f}")


# ── Combinações ──────────────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  COMBINAÇÕES")
print(f"{'='*65}")
combos = [
    ("r15<0.03 AND d_bps>=8",   lambda r: r["r15"] < 0.03 and r["d_bps"] >= 8),
    ("r15<0.03 AND d_bps>=10",  lambda r: r["r15"] < 0.03 and r["d_bps"] >= 10),
    ("r15<0.03 AND d_bps>=12",  lambda r: r["r15"] < 0.03 and r["d_bps"] >= 12),
    ("r30<0.05 AND d_bps>=8",   lambda r: r["r30"] < 0.05 and r["d_bps"] >= 8),
    ("r30<0.05 AND d_bps>=10",  lambda r: r["r30"] < 0.05 and r["d_bps"] >= 10),
    ("safe=True",               lambda r: r["safe"] is True),
    ("safe=True AND d_bps>=10", lambda r: r["safe"] is True and r["d_bps"] >= 10),
]
print(f"  {'Filtro':<32}  {'n':>4}  {'WR':>6}  {'PnL':>8}  {'avg':>7}  {'bloq':>5}  bloq_PnL")
print("  " + "─"*72)
for label, fn in combos:
    sub = [r for r in records if fn(r)]
    blk = [r for r in records if not fn(r)]
    if not sub:
        continue
    sw = sum(1 for r in sub if r["win"])
    sp = round(sum(r["pnl"] for r in sub), 2)
    bp = round(sum(r["pnl"] for r in blk), 2)
    print(f"  {label:<32}  {len(sub):>4}  {sw/len(sub):>6.1%}  {sp:>+8.2f}  {sp/len(sub):>+7.3f}  "
          f"{len(blk):>5}  {bp:+.2f}")
print(f"  {'base':<32}  {n:>4}  {len(wins)/n:>6.1%}  {pnl_t:>+8.2f}  {pnl_t/n:>+7.3f}")


# ── Detalhe LOSS por faixa de d_bps ──────────────────────────────────────────
print(f"\n  LOSS por faixa de distance_to_ptb:")
for lo, hi in [(0,8),(8,12),(12,100)]:
    grp = [r for r in stops if lo <= r["d_bps"] < hi]
    if not grp:
        continue
    gp = round(sum(r["pnl"] for r in grp), 2)
    label = f"{lo}–{hi if hi<100 else '∞'}bps"
    print(f"    {label:<10}  n={len(grp):>3}  PnL={gp:>+7.2f}  avg={gp/len(grp):>+6.3f}")

print(f"\n  LOSS por faixa de range_15s:")
for lo, hi in [(0,0.01),(0.01,0.02),(0.02,0.03),(0.03,1.0)]:
    grp = [r for r in stops if lo <= r["r15"] < hi]
    if not grp:
        continue
    gp = round(sum(r["pnl"] for r in grp), 2)
    label = f"{lo:.2f}–{hi:.2f}"
    print(f"    r15 {label:<10}  n={len(grp):>3}  PnL={gp:>+7.2f}  avg={gp/len(grp):>+6.3f}")
