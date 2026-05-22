import json
from pathlib import Path

sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

trades = []
for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
    entries = {r['slug']: r for r in rows if r.get('type') == 'ee_paper_entry'}
    closed  = {r['slug']: r for r in rows if r.get('type') == 'ee_paper_closed'}
    for slug, e in entries.items():
        cl = closed.get(slug)
        if not cl:
            continue
        outcome = cl['ee']['outcome']
        if outcome in ('MISSED',):
            continue
        trades.append({
            'slug':    slug[-6:],
            'side':    e.get('side'),
            'ep':      e.get('ep', 0),
            'el_vel':  e.get('el', {}).get('el_vel', 0),
            'outcome': outcome,
            'pnl':     cl['ee']['pnl'],
        })

wins   = [t for t in trades if t['outcome'] == 'WIN']
hedges = [t for t in trades if t['outcome'] == 'WIN_HEDGE']

print(f"Total: {len(trades)}  WIN: {len(wins)}  WIN_HEDGE: {len(hedges)}\n")

print(f"{'Outcome':<12} {'avg el_vel':>10} {'min el_vel':>10} {'max el_vel':>10}")
print('-' * 46)
for label, group in [('WIN', wins), ('WIN_HEDGE', hedges)]:
    vels = [t['el_vel'] for t in group]
    print(f"{label:<12} {sum(vels)/len(vels):>10.3f} {min(vels):>10.3f} {max(vels):>10.3f}")

# distribuição por faixas
print('\nDistribuição por faixa de el_vel:')
buckets = [(0.08, 0.10), (0.10, 0.12), (0.12, 0.15), (0.15, 0.20), (0.20, 0.30)]
print(f"  {'Faixa':>14}  {'WIN':>5}  {'HEDGE':>6}  {'WR WIN':>7}")
print('  ' + '-' * 38)
for lo, hi in buckets:
    nw = sum(1 for t in wins   if lo <= t['el_vel'] < hi)
    nh = sum(1 for t in hedges if lo <= t['el_vel'] < hi)
    nt = nw + nh
    wr = f'{nw/nt:.0%}' if nt else 'n/a'
    print(f"  [{lo:.2f} – {hi:.2f})  {nw:>5}  {nh:>6}  {wr:>7}")

# simular gate el_vel > threshold
print('\nSimulação de gate el_vel (só entrar acima do threshold):')
print(f"  {'Gate':>8}  {'Trades':>7}  {'WIN':>5}  {'HEDGE':>6}  {'WR':>7}  {'PnL':>8}")
print('  ' + '-' * 50)
for gate in [0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.15]:
    subset = [t for t in trades if t['el_vel'] >= gate]
    nw = sum(1 for t in subset if t['outcome'] == 'WIN')
    nh = sum(1 for t in subset if t['outcome'] == 'WIN_HEDGE')
    nt = len(subset)
    wr = f'{nw/nt:.0%}' if nt else 'n/a'
    pnl = round(sum(t['pnl'] for t in subset), 2)
    blocked = len(trades) - nt
    print(f"  {gate:>8.2f}  {nt:>7}  {nw:>5}  {nh:>6}  {wr:>7}  {pnl:>+8.2f}  (bloqueia {blocked})")
