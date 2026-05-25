"""Resultados completos EE paper local — corrigido (sem MISSED) + el_vel + versão v1/v2."""
import sys, io, json
from pathlib import Path
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

QTY = 6.0
sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

# Coletar trades com el_vel dos ee_paper_entry
entry_info = {}   # slug -> {el_vel, ep, secs, side}
trades_raw = []
sess_version = {}  # session_name -> 'v1'|'v2'

for lp in sessions:
    sname = lp.parent.name
    is_v2 = False
    for line in lp.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get('type', '')

        if t == 'startup':
            params = e.get('params', {})
            if 'EE_STOP_LEVEL' in params:
                is_v2 = True

        if t == 'ee_paper_entry':
            slug = e.get('slug', '')
            el   = e.get('el', {})
            entry_info[slug] = {
                'el_vel':  float(el.get('el_vel') or 0),
                'el_bid_240': float(el.get('el_bid_240') or 0),
                'el_bid_180': float(el.get('el_bid_180') or 0),
                'ep':      float(e.get('ep') or 0),
                'secs':    e.get('secs'),
                'side':    e.get('side', ''),
                'sess':    sname[-6:],
                'version': 'v2' if is_v2 else 'v1',
            }

        if t == 'ee_paper_closed':
            ee   = e.get('ee', {})
            slug = e.get('slug', '')
            outcome = ee.get('outcome', '')
            pnl     = float(ee.get('pnl') or 0)
            entry   = entry_info.get(slug, {})
            trades_raw.append({
                'slug':    slug,
                'outcome': outcome,
                'pnl':     pnl,
                'ep':      entry.get('ep', float(ee.get('entry_ep') or 0)),
                'secs':    entry.get('secs', ee.get('entry_secs')),
                'side':    entry.get('side', ee.get('entry_side', '')),
                'sess':    sname[-6:],
                'el_vel':  entry.get('el_vel', 0),
                'version': entry.get('version', 'v2' if is_v2 else 'v1'),
            })
    sess_version[sname] = 'v2' if is_v2 else 'v1'

# Separar MISSED (bug de tracking — não perda real)
real_trades = [t for t in trades_raw if t['outcome'] != 'MISSED']
missed      = [t for t in trades_raw if t['outcome'] == 'MISSED']

wins   = [t for t in real_trades if t['outcome'] == 'WIN']
pp     = [t for t in real_trades if t['outcome'] == 'PROFIT_PROTECT']
stop   = [t for t in real_trades if t['outcome'] == 'STOP_LOSS']
hedges = [t for t in real_trades if 'HEDGE' in t['outcome']]
rev    = [t for t in real_trades if t['outcome'] == 'REVERSAL']

pnl_real = sum(t['pnl'] for t in real_trades)

# Versoes
v1_trades = [t for t in real_trades if t['version'] == 'v1']
v2_trades = [t for t in real_trades if t['version'] == 'v2']

print("=== EE PAPER — RESULTADOS LOCAIS CORRIGIDOS ===")
print(f"Sessoes: {len(sessions)}   Trades totais: {len(trades_raw)}")
print(f"  v1 (sem stop/PP): {len([s for s,v in sess_version.items() if v=='v1'])} sessoes  "
      f"{len(v1_trades)} trades")
print(f"  v2 (stop 0.65+PP): {len([s for s,v in sess_version.items() if v=='v2'])} sessoes  "
      f"{len(v2_trades)} trades")
print(f"  MISSED (excluídos): {len(missed)}")
print()

print(f"{'='*55}")
print(f"TOTAL (sem MISSED): {len(real_trades)} trades")
print(f"{'='*55}")
outcomes_c = Counter(t['outcome'] for t in real_trades)
for k, n in outcomes_c.most_common():
    pnl_k = sum(t['pnl'] for t in real_trades if t['outcome'] == k)
    print(f"  {k:<18} {n:>3}   PnL={pnl_k:+.4f}")
print()
wr_pct = (len(wins) + len(pp)) / len(real_trades) * 100 if real_trades else 0
print(f"WR (WIN+PP / total): {len(wins)+len(pp)}/{len(real_trades)} = {wr_pct:.1f}%")
print(f"PnL total:  {pnl_real:+.4f}")
print(f"avg/trade:  {pnl_real/len(real_trades):+.4f}")
print()

# Por versão
if v1_trades:
    p1 = sum(t['pnl'] for t in v1_trades)
    w1 = sum(1 for t in v1_trades if 'WIN' in t['outcome'] and 'HEDGE' not in t['outcome'])
    print(f"v1 ({len(v1_trades)} trades): WR={w1}/{len(v1_trades)}={w1/len(v1_trades)*100:.0f}%  PnL={p1:+.4f}  avg={p1/len(v1_trades):+.4f}")
if v2_trades:
    p2 = sum(t['pnl'] for t in v2_trades)
    w2 = sum(1 for t in v2_trades if t['outcome'] in ('WIN','PROFIT_PROTECT'))
    print(f"v2 ({len(v2_trades)} trades): WR={w2}/{len(v2_trades)}={w2/len(v2_trades)*100:.0f}%  PnL={p2:+.4f}  avg={p2/len(v2_trades):+.4f}")
print()

# el_vel por outcome
print("--- el_vel médio por outcome ---")
for k in ('WIN', 'PROFIT_PROTECT', 'WIN_HEDGE', 'WIN_BOUNCE', 'STOP_LOSS', 'REVERSAL'):
    grp = [t for t in real_trades if t['outcome'] == k and t['el_vel'] > 0]
    if grp:
        avg_vel = sum(t['el_vel'] for t in grp) / len(grp)
        print(f"  {k:<18} n={len(grp)}  avg_el_vel={avg_vel:.4f}")
print()

# Entry price breakdown
print("--- PnL por faixa de entry price (sem MISSED) ---")
for lo, hi in [(0.82,0.83),(0.83,0.84),(0.84,0.85),(0.85,0.86),(0.86,0.87)]:
    grp = [t for t in real_trades if lo <= t['ep'] < hi]
    if grp:
        pg = sum(t['pnl'] for t in grp)
        wg = sum(1 for t in grp if t['outcome'] in ('WIN','PROFIT_PROTECT'))
        print(f"  [{lo:.2f},{hi:.2f})  n={len(grp):>2}  WR={wg/len(grp)*100:.0f}%  PnL={pg:+.4f}")

print()
# Gate el_vel >= 0.11 (proposto no doc para filtrar WIN_HEDGE)
all_hedge = [t for t in real_trades if 'HEDGE' in t['outcome'] or t['outcome'] == 'WIN_BOUNCE']
print(f"--- Gate el_vel >= 0.11 (proposto para filtrar {len(all_hedge)} WIN_HEDGE/WIN_BOUNCE) ---")
hedge_blocked = [t for t in all_hedge if t['el_vel'] < 0.11]
hedge_pass    = [t for t in all_hedge if t['el_vel'] >= 0.11]
wins_blocked  = [t for t in wins if t['el_vel'] < 0.11]
print(f"  WIN_HEDGE/BOUNCE com el_vel < 0.11 (bloqueados): {len(hedge_blocked)}")
print(f"  WIN_HEDGE/BOUNCE com el_vel >= 0.11 (passariam):  {len(hedge_pass)}")
print(f"  WIN trades com el_vel < 0.11 (custo do gate):     {len(wins_blocked)}")
if hedge_blocked:
    saving = sum(t['pnl'] for t in hedge_blocked)  # negative = losses avoided
    print(f"  PnL salvo bloqueando hedge_blocked: {-saving:+.4f}")
if wins_blocked:
    cost = sum(t['pnl'] for t in wins_blocked)
    print(f"  PnL perdido bloqueando wins:        {-cost:+.4f}")
