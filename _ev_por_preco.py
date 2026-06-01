"""EV por preco de entrada — EE simulation."""
import glob, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from collections import defaultdict
from _sim_ee_gates import load_snaps, simulate_slug

by_slug = load_snaps('logs/ee_paper_*/ee_paper.jsonl')
trades = []
for slug, snaps in sorted(by_slug.items()):
    r = simulate_slug(snaps, 0.17, None, True, True, 6.0)
    if r:
        r['slug'] = slug
        trades.append(r)

by_ep = defaultdict(list)
for t in trades:
    ep_band = round(t['ep'] * 100) / 100
    by_ep[ep_band].append(t)

print('=== EE SIM (vel>=0.17, sem spread, sem n5/hora) - por preco ===')
print(f'Total: {len(trades)} trades')
print(f"  ep     n    W    L    WR%    EV       pnl/trade")
for ep_val in sorted(by_ep.keys()):
    g = by_ep[ep_val]
    n = len(g)
    w = sum(1 for t in g if t['pnl'] > 0)
    l = n - w
    wr = w / n * 100
    ev = wr / 100 - ep_val
    avg_pnl = sum(t['pnl'] for t in g) / n
    flag = 'EV+' if ev > 0 else 'EV-'
    print(f"  {ep_val:.2f}  {n:>4}  {w:>4}  {l:>4}  {wr:>5.1f}%  {ev:>+6.4f}   {avg_pnl:>+8.3f}  {flag}")
