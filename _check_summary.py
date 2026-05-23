import json
from pathlib import Path
from collections import Counter

sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

trades = []
for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
    entries = {r['slug']: r for r in rows if r.get('type') == 'ee_paper_entry'}
    closed  = {r['slug']: r for r in rows if r.get('type') == 'ee_paper_closed'}
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
        })

closed_t = [t for t in trades if t['outcome'] != 'aberta' and t['outcome'] != 'MISSED']
abertas  = [t for t in trades if t['outcome'] == 'aberta']

counts  = Counter(t['outcome'] for t in closed_t)
pnl_tot = round(sum(t['pnl'] for t in closed_t), 2)
n       = len(closed_t)
n_pos   = sum(1 for t in closed_t if t['pnl'] > 0)

print(f"\n{'='*55}")
print(f"  RUNNER EE — ACUMULADO")
print(f"{'='*55}")
print(f"  Sessoes       : {len(sessions)}")
print(f"  Trades fechados: {n}  ({len(abertas)} abertas agora)")
print(f"  WIN           : {counts['WIN']}")
print(f"  PROFIT_PROTECT: {counts['PROFIT_PROTECT']}")
print(f"  WIN_HEDGE     : {counts['WIN_HEDGE']}")
print(f"  STOP_LOSS     : {counts['STOP_LOSS']}")
print(f"  REVERSAL      : {counts.get('REVERSAL', 0)}")
print(f"  WR (lucrativos): {n_pos}/{n} = {n_pos/n:.1%}")
print(f"  PnL total     : {pnl_tot:>+.2f}")
print(f"  avg / trade   : {pnl_tot/n:>+.3f}")
print()

# STOP_LOSS — detalhe do gap
stops = [t for t in closed_t if t['outcome'] == 'STOP_LOSS']
if stops:
    print(f"  STOP_LOSS detalhe (exit real vs stop 0.65 esperado):")
    for t in stops:
        exit_price = round(t['ep'] + t['pnl'] / 6, 3)
        gap = round(0.65 - exit_price, 3)
        expected_pnl = round((0.65 - t['ep']) * 6, 2)
        print(f"    {t['slug']}  EP={t['ep']:.3f}  exit_real={exit_price:.3f}  "
              f"gap={gap:+.3f}  PnL={t['pnl']:>+.2f}  (esperado {expected_pnl:>+.2f})")

# PP — distribuição de preços de saída
pps = [t for t in closed_t if t['outcome'] == 'PROFIT_PROTECT']
if pps:
    exits = [round(t['ep'] + t['pnl'] / 6, 3) for t in pps]
    print(f"\n  PROFIT_PROTECT: {len(pps)} trades")
    print(f"    exit range: {min(exits):.3f} – {max(exits):.3f}")
    print(f"    PnL range : {min(t['pnl'] for t in pps):>+.2f} – {max(t['pnl'] for t in pps):>+.2f}")
    print(f"    PnL total : {sum(t['pnl'] for t in pps):>+.2f}")
