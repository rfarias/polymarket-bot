"""Resumo completo: acumulado, por sessão e el_vel vs outcome."""
import json
from pathlib import Path
from collections import Counter, defaultdict

QTY = 6.0
sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

trades = []
for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
    entries = {r['slug']: r for r in rows if r.get('type') == 'ee_paper_entry'}
    closed  = {r['slug']: r for r in rows if r.get('type') == 'ee_paper_closed'}
    startup = next((r for r in rows if r.get('type') == 'startup'), None)
    ts_start = startup.get('ts', 0) if startup else 0

    for slug, e in entries.items():
        cl = closed.get(slug)
        outcome = cl['ee']['outcome'] if cl else 'aberta'
        pnl     = cl['ee']['pnl']    if cl else None
        trades.append({
            'slug':    slug[-6:],
            'ep':      e.get('ep', 0),
            'el_vel':  e.get('el', {}).get('el_vel', 0),
            'outcome': outcome,
            'pnl':     pnl,
            'session': lp.parent.name,
            'ts':      e.get('ts', ts_start),
        })

closed_t = [t for t in trades if t['outcome'] not in ('aberta', 'MISSED')]

# --- Acumulado geral ---
c = Counter(t['outcome'] for t in closed_t)
pnl_tot = round(sum(t['pnl'] for t in closed_t), 2)
n = len(closed_t)
lucro = sum(1 for t in closed_t if t['pnl'] > 0)

print(f"\n{'='*60}")
print(f"  ACUMULADO TOTAL — {n} trades fechados")
print(f"{'='*60}")
print(f"  WIN            : {c['WIN']}")
print(f"  PROFIT_PROTECT : {c['PROFIT_PROTECT']}")
print(f"  WIN_HEDGE      : {c['WIN_HEDGE']}")
print(f"  STOP_LOSS      : {c['STOP_LOSS']}")
print(f"  REVERSAL       : {c.get('REVERSAL',0)}")
print(f"  WR (lucro>0)   : {lucro}/{n} = {lucro/n:.1%}")
print(f"  PnL total      : {pnl_tot:>+.2f}")
print(f"  avg / trade    : {pnl_tot/n:>+.3f}")

# --- Primeiros 43 vs novos 79 ---
old_t = closed_t[:43]
new_t = closed_t[43:]
print(f"\n  Comparação períodos:")
for label, group in [('1ª amostra (43 trades)', old_t), ('Adicionais (79 trades)', new_t)]:
    co = Counter(t['outcome'] for t in group)
    pnl = round(sum(t['pnl'] for t in group), 2)
    nl = sum(1 for t in group if t['pnl'] > 0)
    stop_rate = co['STOP_LOSS'] / len(group) if group else 0
    print(f"  {label}: WR={nl/len(group):.1%}  PnL={pnl:>+.2f}  "
          f"STOP={co['STOP_LOSS']} ({stop_rate:.1%})  PP={co['PROFIT_PROTECT']}")

# --- el_vel por outcome ---
print(f"\n{'='*60}")
print(f"  el_vel POR OUTCOME")
print(f"{'='*60}")
by_out = defaultdict(list)
for t in closed_t:
    by_out[t['outcome']].append(t['el_vel'])
for out in ['WIN', 'PROFIT_PROTECT', 'STOP_LOSS', 'WIN_HEDGE', 'REVERSAL']:
    vels = by_out.get(out, [])
    if not vels: continue
    print(f"  {out:<16}: n={len(vels):>3}  avg={sum(vels)/len(vels):.3f}  "
          f"min={min(vels):.3f}  max={max(vels):.3f}")

# --- Simulação gate el_vel nos 122 trades ---
print(f"\n{'='*60}")
print(f"  SIMULAÇÃO GATE el_vel (amostra completa 122 trades)")
print(f"{'='*60}")
print(f"  {'Gate':>8}  {'n':>5}  {'WR':>7}  {'STOP':>6}  {'STOP%':>7}  {'PnL':>9}  {'avg':>7}")
print("  " + "-"*55)
for gate in [0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.15]:
    sub = [t for t in closed_t if t['el_vel'] >= gate]
    if not sub: continue
    nl = sum(1 for t in sub if t['pnl'] > 0)
    ns = sum(1 for t in sub if t['outcome'] == 'STOP_LOSS')
    pnl = round(sum(t['pnl'] for t in sub), 2)
    blk = len(closed_t) - len(sub)
    print(f"  {gate:>8.2f}  {len(sub):>5}  {nl/len(sub):>7.1%}  {ns:>6}  "
          f"{ns/len(sub):>7.1%}  {pnl:>+9.2f}  {pnl/len(sub):>+7.3f}  (bloqueia {blk})")

print(f"  {'base':>8}  {n:>5}  {lucro/n:>7.1%}  {c['STOP_LOSS']:>6}  "
      f"{c['STOP_LOSS']/n:>7.1%}  {pnl_tot:>+9.2f}  {pnl_tot/n:>+7.3f}")

# --- Stops: gap distribution ---
stops = [t for t in closed_t if t['outcome'] == 'STOP_LOSS']
gaps = [round(0.65 - (t['ep'] + t['pnl']/QTY), 3) for t in stops]
print(f"\n  STOP_LOSS gaps (fill real vs 0.65 esperado):")
print(f"    n={len(stops)}  avg_gap={sum(gaps)/len(gaps):.3f}  "
      f"min={min(gaps):.3f}  max={max(gaps):.3f}")
buckets = [(0,0.02),(0.02,0.05),(0.05,0.10),(0.10,0.20),(0.20,0.50)]
for lo,hi in buckets:
    n_b = sum(1 for g in gaps if lo<=g<hi)
    print(f"    gap [{lo:.2f}-{hi:.2f}): {n_b} trades")
