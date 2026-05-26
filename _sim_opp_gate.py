"""
_sim_opp_gate.py
Analisa o opp_bid e a distancia ate PP no momento da entrada.
Hipotese: mercado ainda contestado (opp alto) = mais stops.
Simula gates: opp_bid < X e distancia_pp = (0.88 - ep) > Y.
"""
import json
from pathlib import Path
from collections import defaultdict

QTY      = 6.0
PP_BID   = 0.88

sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

records = []

for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]

    slug_snaps   = defaultdict(list)
    paper_entries = {}
    paper_closed  = {}

    for r in rows:
        t = r.get('type')
        if t == 'snapshot':
            sl   = r.get('current_slug', '')
            sc   = r.get('current_secs')
            ex   = r.get('current_exec', {})
            if sl and sc is not None:
                slug_snaps[sl].append({
                    'secs':    sc,
                    'up_bid':  ex.get('up_bid', 0),
                    'down_bid': ex.get('down_bid', 0),
                })
        elif t == 'ee_paper_entry':
            paper_entries[r.get('slug', '')] = r
        elif t == 'ee_paper_closed':
            paper_closed[r.get('slug', '')] = r

    for slug, entry in paper_entries.items():
        closed = paper_closed.get(slug)
        if not closed:
            continue
        ee = closed.get('ee', {})
        outcome = ee.get('outcome', '?')
        if outcome in ('aberta', 'MISSED'):
            continue

        ep       = entry.get('ep', 0)
        side     = entry.get('side', '')
        entry_secs = entry.get('secs', 180)
        el_vel   = entry.get('el', {}).get('el_vel', 0)
        pnl      = ee.get('pnl', 0)

        if not ep or not side:
            continue

        # Snap mais proximo ao momento da entrada
        snaps = slug_snaps.get(slug, [])
        entry_snap = min(snaps, key=lambda s: abs(s['secs'] - entry_secs)) if snaps else None
        if not entry_snap:
            continue

        if side == 'UP':
            opp_bid = entry_snap['down_bid']
        else:
            opp_bid = entry_snap['up_bid']

        # Metricas derivadas
        dist_pp   = round(PP_BID - ep, 3)          # distancia ate PP trigger
        el_opp_sum = round(ep + opp_bid, 3)         # soma EL+OPP (mercado contestado se alto)
        opp_pct   = round(opp_bid / ep, 3)          # OPP como % do EP

        records.append({
            'slug':       slug[-6:],
            'ep':         ep,
            'side':       side,
            'el_vel':     el_vel,
            'opp_bid':    round(opp_bid, 3),
            'dist_pp':    dist_pp,
            'el_opp_sum': el_opp_sum,
            'outcome':    outcome,
            'pnl':        pnl,
        })

n_total = len(records)
print(f"\n{'='*70}")
print(f"  OPP_BID E DIST_PP NA ENTRADA — {n_total} trades")
print(f"{'='*70}")

# --- 1. Distribuicao de opp_bid por outcome ---
from collections import defaultdict
import statistics

by_out = defaultdict(list)
for r in records:
    by_out[r['outcome']].append(r['opp_bid'])

print(f"\n  opp_bid por outcome:")
print(f"  {'Outcome':<16}  {'n':>3}  {'avg':>6}  {'med':>6}  {'min':>6}  {'max':>6}")
print("  " + "-"*46)
for out in ['WIN','PROFIT_PROTECT','STOP_LOSS','WIN_HEDGE','REVERSAL']:
    vals = by_out.get(out, [])
    if not vals: continue
    print(f"  {out:<16}  {len(vals):>3}  {sum(vals)/len(vals):>6.3f}  "
          f"{statistics.median(vals):>6.3f}  {min(vals):>6.3f}  {max(vals):>6.3f}")

# --- 2. Distribuicao de dist_pp por outcome ---
by_out_pp = defaultdict(list)
for r in records:
    by_out_pp[r['outcome']].append(r['dist_pp'])

print(f"\n  dist_pp (0.88 - ep) por outcome:")
print(f"  {'Outcome':<16}  {'n':>3}  {'avg':>6}  {'med':>6}  {'min':>6}  {'max':>6}")
print("  " + "-"*46)
for out in ['WIN','PROFIT_PROTECT','STOP_LOSS','WIN_HEDGE','REVERSAL']:
    vals = by_out_pp.get(out, [])
    if not vals: continue
    print(f"  {out:<16}  {len(vals):>3}  {sum(vals)/len(vals):>6.3f}  "
          f"{statistics.median(vals):>6.3f}  {min(vals):>6.3f}  {max(vals):>6.3f}")

# --- 3. Gate opp_bid < X ---
print(f"\n{'='*70}")
print(f"  GATE opp_bid < X (mercado pouco contestado)")
print(f"{'='*70}")
print(f"  {'Gate opp<':>10}  {'n':>4}  {'WR':>7}  {'STOP':>5}  {'STOP%':>6}  {'PnL':>8}  {'avg':>7}  bloq")
print("  " + "-"*62)
for gate in [0.25, 0.22, 0.20, 0.18, 0.16, 0.15]:
    sub = [r for r in records if r['opp_bid'] < gate]
    if not sub: continue
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl'] > 0)
    stops = sum(1 for r in sub if r['outcome'] == 'STOP_LOSS')
    pnl = round(sum(r['pnl'] for r in sub), 2)
    blk = n_total - n
    print(f"  {gate:>10.2f}  {n:>4}  {wins/n:>7.1%}  {stops:>5}  {stops/n:>6.1%}  {pnl:>+8.2f}  {pnl/n:>+7.3f}  ({blk})")
# base
n = n_total
wins = sum(1 for r in records if r['pnl'] > 0)
stops = sum(1 for r in records if r['outcome'] == 'STOP_LOSS')
pnl_base = round(sum(r['pnl'] for r in records), 2)
print(f"  {'base':>10}  {n:>4}  {wins/n:>7.1%}  {stops:>5}  {stops/n:>6.1%}  {pnl_base:>+8.2f}  {pnl_base/n:>+7.3f}")

# --- 4. Gate dist_pp > Y ---
print(f"\n{'='*70}")
print(f"  GATE dist_pp > Y (distancia minima ate PP)")
print(f"{'='*70}")
print(f"  {'Gate pp>':>10}  {'n':>4}  {'WR':>7}  {'STOP':>5}  {'STOP%':>6}  {'PnL':>8}  {'avg':>7}  bloq")
print("  " + "-"*62)
for gate in [0.02, 0.03, 0.04, 0.05, 0.06]:
    sub = [r for r in records if r['dist_pp'] >= gate]
    if not sub: continue
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl'] > 0)
    stops = sum(1 for r in sub if r['outcome'] == 'STOP_LOSS')
    pnl = round(sum(r['pnl'] for r in sub), 2)
    blk = n_total - n
    print(f"  {gate:>10.2f}  {n:>4}  {wins/n:>7.1%}  {stops:>5}  {stops/n:>6.1%}  {pnl:>+8.2f}  {pnl/n:>+7.3f}  ({blk})")

# --- 5. Combinacao: el_vel >= 0.13 + opp_bid < 0.20 ---
print(f"\n{'='*70}")
print(f"  COMBINACOES (el_vel + opp_bid)")
print(f"{'='*70}")
print(f"  {'Cenario':<30}  {'n':>4}  {'WR':>7}  {'STOP%':>6}  {'PnL':>8}  {'avg':>7}")
print("  " + "-"*62)
combos = [
    ('base', lambda r: True),
    ('el_vel>=0.13', lambda r: r['el_vel'] >= 0.13),
    ('opp<0.20', lambda r: r['opp_bid'] < 0.20),
    ('opp<0.18', lambda r: r['opp_bid'] < 0.18),
    ('el_vel>=0.13 + opp<0.20', lambda r: r['el_vel'] >= 0.13 and r['opp_bid'] < 0.20),
    ('el_vel>=0.13 + opp<0.18', lambda r: r['el_vel'] >= 0.13 and r['opp_bid'] < 0.18),
    ('el_vel>=0.12 + opp<0.20', lambda r: r['el_vel'] >= 0.12 and r['opp_bid'] < 0.20),
]
for label, fn in combos:
    sub = [r for r in records if fn(r)]
    if not sub: continue
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl'] > 0)
    stops = sum(1 for r in sub if r['outcome'] == 'STOP_LOSS')
    pnl = round(sum(r['pnl'] for r in sub), 2)
    print(f"  {label:<30}  {n:>4}  {wins/n:>7.1%}  {stops/n:>6.1%}  {pnl:>+8.2f}  {pnl/n:>+7.3f}")

# --- 6. Detalhe dos STOP_LOSS: opp_bid vs wins ---
print(f"\n  STOP_LOSS — opp_bid e dist_pp no momento da entrada:")
stops = [r for r in records if r['outcome'] == 'STOP_LOSS']
stops_sorted = sorted(stops, key=lambda r: r['opp_bid'], reverse=True)
print(f"  {'Slug':>6}  {'EP':>5}  {'opp_bid':>7}  {'dist_pp':>7}  {'el_vel':>6}  {'PnL':>7}")
print("  " + "-"*46)
for r in stops_sorted:
    print(f"  {r['slug']:>6}  {r['ep']:.3f}  {r['opp_bid']:>7.3f}  {r['dist_pp']:>7.3f}  {r['el_vel']:>6.3f}  {r['pnl']:>+7.2f}")
avg_opp_stop = sum(r['opp_bid'] for r in stops) / len(stops)
avg_opp_win  = sum(r['opp_bid'] for r in records if r['outcome'] != 'STOP_LOSS') / max(1, sum(1 for r in records if r['outcome'] != 'STOP_LOSS'))
print(f"\n  opp_bid medio:  stops={avg_opp_stop:.3f}  nao-stops={avg_opp_win:.3f}  diff={avg_opp_stop-avg_opp_win:+.3f}")
