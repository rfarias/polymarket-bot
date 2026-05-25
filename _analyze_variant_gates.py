"""
Analisa gates por variante do runner AR real.
Cruza enter events (sinal) com trade_closed/flat/redeem_flat (PnL).
"""
import json, sys, io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

QTY = 6.0
sessions = sorted(Path("logs").glob("current_almost_resolved_real_*/current_almost_resolved_real.jsonl"))

enters_by_slug = {}
for lp in sessions:
    try:
        rows = [json.loads(l) for l in lp.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except Exception:
        continue
    for r in rows:
        if r.get("type") != "enter":
            continue
        sig = r.get("signal") or {}
        tr  = r.get("trade") or {}
        slug = sig.get("event_slug") or tr.get("event_slug") or ""
        if not slug:
            continue
        enters_by_slug[slug] = {
            "variant":   sig.get("setup_variant") or "unknown",
            "ep":        sig.get("entry_price") or tr.get("entry_price") or 0.0,
            "secs":      sig.get("secs_to_end"),
            "d_bps":     sig.get("distance_to_price_to_beat_bps"),
            "safe_dist": sig.get("passive_capture_safe_distance_ok"),
            "r15":       sig.get("market_range_15s"),
            "r30":       sig.get("market_range_30s"),
            "stop_p":    tr.get("stop_price") or sig.get("stop_price"),
        }

closes_by_slug = {}
for lp in sessions:
    try:
        rows = [json.loads(l) for l in lp.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except Exception:
        continue
    for c in (r for r in rows if r.get("type") == "trade_closed"):
        slug = c.get("entry_slug") or ""
        ep = c.get("entry_price") or 0.0
        xp = c.get("exit_price")
        qty = c.get("qty") or QTY
        pnl = c.get("pnl_usd")
        rsn = c.get("last_reason") or ""
        if pnl is None and xp is not None and ep:
            pnl = round((xp - ep) * qty, 4)
        if pnl is None or not ep or not slug:
            continue
        closes_by_slug[slug] = {"pnl": pnl, "xp": xp, "ep": ep, "rsn": rsn}
    for e in (r for r in rows if r.get("type") == "flat"):
        tr = e.get("trade") or {}
        slug = tr.get("event_slug") or ""
        ep = tr.get("entry_price") or 0.0
        xp = tr.get("exit_price_posted")
        xq = tr.get("exit_qty_filled") or 0.0
        eq = tr.get("entry_qty_filled") or 0.0
        rsn = tr.get("last_reason") or ""
        if not slug or slug in closes_by_slug or xp is None or not ep or eq <= 0:
            continue
        if xp <= 0.01 and xq <= 0:
            continue
        qty = xq if xq > 0 else eq
        closes_by_slug[slug] = {"pnl": round((xp - ep) * qty, 4), "xp": xp, "ep": ep, "rsn": rsn}
    for e in (r for r in rows if r.get("type") == "redeem_flat"):
        tr = e.get("trade") or {}
        slug = tr.get("event_slug") or ""
        ep = tr.get("entry_price") or 0.0
        xp = e.get("exit_price")
        pnl = e.get("pnl_usd")
        rsn = tr.get("last_reason") or ""
        qty = tr.get("entry_qty_filled") or QTY
        if not slug or slug in closes_by_slug:
            continue
        if pnl is None and xp is not None and ep:
            pnl = round((xp - ep) * qty, 4)
        if pnl is None or not ep:
            continue
        closes_by_slug[slug] = {"pnl": pnl, "xp": xp, "ep": ep, "rsn": rsn}

records = []
for slug, sig_data in enters_by_slug.items():
    cl = closes_by_slug.get(slug)
    if not cl:
        continue
    records.append({**sig_data, **cl, "win": cl["pnl"] > 0, "slug": slug[-8:]})

# ── STANDARD ────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  STANDARD — gates por entry_price e d_bps")
print("=" * 70)
std = [r for r in records if r["variant"] == "standard"]
pnl_std = round(sum(r["pnl"] for r in std), 2)
print(f"  Base: n={len(std)}  WR={sum(r['win'] for r in std)/len(std):.0%}  PnL={pnl_std:+.2f}\n")

print("  Gate entry_price < X:")
print(f"  {'Gate':>7}  {'n':>4}  {'WR':>6}  {'PnL':>8}  {'avg':>7}  {'bloq':>5}  bloq_PnL")
print("  " + "-" * 55)
for thr in [0.95, 0.96, 0.97, 0.975]:
    sub = [r for r in std if r["ep"] < thr]
    blk = [r for r in std if r["ep"] >= thr]
    if not sub:
        continue
    w = sum(r["win"] for r in sub)
    p = round(sum(r["pnl"] for r in sub), 2)
    bp = round(sum(r["pnl"] for r in blk), 2)
    print(f"  {'<'+str(thr):>7}  {len(sub):>4}  {w/len(sub):>6.0%}  {p:>+8.2f}  {p/len(sub):>+7.3f}  {len(blk):>5}  {bp:+.2f}")
print(f"  {'base':>7}  {len(std):>4}  {sum(r['win'] for r in std)/len(std):>6.0%}  {pnl_std:>+8.2f}  {pnl_std/len(std):>+7.3f}")

print(f"\n  Gate d_bps >= X:")
print(f"  {'Gate':>8}  {'n':>4}  {'WR':>6}  {'PnL':>8}  {'avg':>7}  {'bloq':>5}  bloq_PnL")
print("  " + "-" * 55)
std_d = [r for r in std if r.get("d_bps") is not None]
for thr in [8, 10, 12, 15, 20]:
    sub = [r for r in std_d if r["d_bps"] >= thr]
    blk = [r for r in std_d if r["d_bps"] < thr]
    if not sub:
        continue
    w = sum(r["win"] for r in sub)
    p = round(sum(r["pnl"] for r in sub), 2)
    bp = round(sum(r["pnl"] for r in blk), 2)
    print(f"  {f'>={thr}':>8}  {len(sub):>4}  {w/len(sub):>6.0%}  {p:>+8.2f}  {p/len(sub):>+7.3f}  {len(blk):>5}  {bp:+.2f}")

print(f"\n  Combinacoes ep + d_bps:")
combos = [
    ("ep<0.97 AND d>=8",   lambda r: r["ep"] < 0.97 and (r.get("d_bps") or 0) >= 8),
    ("ep<0.97 AND d>=10",  lambda r: r["ep"] < 0.97 and (r.get("d_bps") or 0) >= 10),
    ("ep<0.97 AND d>=12",  lambda r: r["ep"] < 0.97 and (r.get("d_bps") or 0) >= 12),
    ("ep<0.975 AND d>=10", lambda r: r["ep"] < 0.975 and (r.get("d_bps") or 0) >= 10),
    ("ep<0.975 AND d>=12", lambda r: r["ep"] < 0.975 and (r.get("d_bps") or 0) >= 12),
    ("safe=True",          lambda r: r.get("safe_dist") is True),
    ("ep<0.975 AND safe",  lambda r: r["ep"] < 0.975 and r.get("safe_dist") is True),
]
print(f"  {'Filtro':<28}  {'n':>4}  {'WR':>6}  {'PnL':>8}  {'avg':>7}  {'bloq':>5}  bloq_PnL")
print("  " + "-" * 70)
for label, fn in combos:
    sub = [r for r in std if fn(r)]
    blk = [r for r in std if not fn(r)]
    if not sub:
        continue
    w = sum(r["win"] for r in sub)
    p = round(sum(r["pnl"] for r in sub), 2)
    bp = round(sum(r["pnl"] for r in blk), 2)
    print(f"  {label:<28}  {len(sub):>4}  {w/len(sub):>6.0%}  {p:>+8.2f}  {p/len(sub):>+7.3f}  {len(blk):>5}  {bp:+.2f}")

# ── DUAL_RICH_LATE_LIMIT ────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  DUAL_RICH_LATE_LIMIT — gates por entry_price")
print("=" * 70)
dual = [r for r in records if r["variant"] == "dual_rich_late_limit"]
pnl_dual = round(sum(r["pnl"] for r in dual), 2)
bp0 = [r for r in dual if abs(r["pnl"]) < 0.001]
real_loss = [r for r in dual if r["pnl"] < -0.001]
wins_d = [r for r in dual if r["pnl"] > 0.001]
print(f"  Base: n={len(dual)}  WR={sum(r['win'] for r in dual)/len(dual):.0%}  PnL={pnl_dual:+.2f}")
print(f"  Breakdown: wins={len(wins_d)} (+{sum(r['pnl'] for r in wins_d):.2f})"
      f"  stops={len(real_loss)} ({sum(r['pnl'] for r in real_loss):+.2f})"
      f"  breakeven={len(bp0)} (pnl=0)")
print(f"  Breakeven ep medio: {sum(r['ep'] for r in bp0)/max(1,len(bp0)):.3f}  "
      f"(entram a 0.99, saem a 0.99 = zero lucro)")
print()
print("  Gate entry_price < X:")
print(f"  {'Gate':>7}  {'n':>4}  {'WR':>6}  {'PnL':>8}  {'avg':>7}  {'bloq':>5}  bloq_PnL")
print("  " + "-" * 55)
for thr in [0.985, 0.98, 0.975, 0.97]:
    sub = [r for r in dual if r["ep"] < thr]
    blk = [r for r in dual if r["ep"] >= thr]
    if not sub:
        continue
    w = sum(r["win"] for r in sub)
    p = round(sum(r["pnl"] for r in sub), 2)
    bp = round(sum(r["pnl"] for r in blk), 2)
    print(f"  {'<'+str(thr):>7}  {len(sub):>4}  {w/len(sub):>6.0%}  {p:>+8.2f}  {p/len(sub):>+7.3f}  {len(blk):>5}  {bp:+.2f}")
print(f"  {'base':>7}  {len(dual):>4}  {sum(r['win'] for r in dual)/len(dual):>6.0%}  {pnl_dual:>+8.2f}  {pnl_dual/len(dual):>+7.3f}")

# ── CONTROLLED_LATE_ENTRY ───────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  CONTROLLED_LATE_ENTRY")
print("=" * 70)
cle = [r for r in records if r["variant"] == "controlled_late_entry"]
if cle:
    p_cle = round(sum(r["pnl"] for r in cle), 2)
    print(f"  n={len(cle)}  WR={sum(r['win'] for r in cle)/len(cle):.0%}  PnL={p_cle:+.2f}  avg={p_cle/len(cle):+.3f}")
    print(f"  Positivo — manter sem restricoes adicionais.")

# ── RESUMO ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  RESUMO — impacto dos gates propostos")
print("=" * 70)
std_keep  = [r for r in std  if r["ep"] < 0.97 and (r.get("d_bps") or 0) >= 10]
std_block = [r for r in std  if not (r["ep"] < 0.97 and (r.get("d_bps") or 0) >= 10)]
dual_keep  = [r for r in dual if r["ep"] < 0.985]
dual_block = [r for r in dual if r["ep"] >= 0.985]
cle_keep   = cle

all_keep  = std_keep + dual_keep + cle_keep
all_block = std_block + dual_block
n_k = len(all_keep)
p_k = round(sum(r["pnl"] for r in all_keep), 2)
p_b = round(sum(r["pnl"] for r in all_block), 2)
base_all = [r for r in records if r["variant"] in ("standard", "dual_rich_late_limit", "controlled_late_entry")]
p_base = round(sum(r["pnl"] for r in base_all), 2)

print(f"  Gates:")
print(f"    standard          : ep < 0.97  AND  d_bps >= 10")
print(f"    dual_rich_late    : ep < 0.985 (bloqueia entradas a 0.99 sem margem)")
print(f"    controlled_late   : sem restricao adicional")
print()
print(f"  {'':32}  {'n':>4}  {'WR':>6}  {'PnL':>8}  {'avg':>7}")
print(f"  {'Base (sem gates)':32}  {len(base_all):>4}  "
      f"{sum(r['win'] for r in base_all)/len(base_all):>6.0%}  {p_base:>+8.2f}  {p_base/len(base_all):>+7.3f}")
print(f"  {'Com gates (mantidos)':32}  {n_k:>4}  "
      f"{sum(r['win'] for r in all_keep)/n_k:>6.0%}  {p_k:>+8.2f}  {p_k/n_k:>+7.3f}")
print(f"  {'Bloqueados':32}  {len(all_block):>4}  "
      f"  {'':6}  {p_b:>+8.2f}")
print(f"\n  Melhoria: {p_k - p_base:+.2f} (de {p_base:+.2f} para {p_k:+.2f})")
