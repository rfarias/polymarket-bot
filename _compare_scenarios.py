"""Compara todos os cenarios testados: gate el_vel, dia da semana, runner real."""
import json
import datetime
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

    dt  = datetime.datetime.fromtimestamp(ts_start) if ts_start else None
    dow = dt.strftime('%a') if dt else '?'

    for slug, e in entries.items():
        cl = closed.get(slug)
        outcome = cl['ee']['outcome'] if cl else 'aberta'
        pnl     = cl['ee']['pnl']    if cl else None
        if outcome in ('aberta', 'MISSED'):
            continue
        trades.append({
            'ep':      e.get('ep', 0),
            'el_vel':  e.get('el', {}).get('el_vel', 0),
            'outcome': outcome,
            'pnl':     pnl,
            'dow':     dow,
        })

def stats(grp, label=''):
    n = len(grp)
    if n == 0:
        return
    lucro = sum(1 for t in grp if t['pnl'] > 0)
    stops = sum(1 for t in grp if t['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(t['pnl'] for t in grp), 2)
    print(f"  {label:<30}  n={n:>3}  WR={lucro/n:>6.1%}  "
          f"STOP={stops:>2}({stops/n:>5.1%})  PnL={pnl:>+8.2f}  avg={pnl/n:>+7.3f}")

# ── 1. Cenarios de gate el_vel ─────────────────────────────────────────────────
print("=" * 75)
print("  1. GATE el_vel — impacto nos 122 trades")
print("=" * 75)
print(f"  {'Cenario':<30}  {'n':>3}  {'WR':>7}  {'STOP':>9}  {'PnL':>9}  {'avg':>8}")
print("  " + "-" * 65)
for gate, label in [
    (0.08, 'Base (sem gate)'),
    (0.09, 'Gate 0.09'),
    (0.10, 'Gate 0.10'),
    (0.11, 'Gate 0.11'),
    (0.12, 'Gate 0.12'),
    (0.13, 'Gate 0.13 [candidato]'),
    (0.15, 'Gate 0.15'),
]:
    sub = [t for t in trades if t['el_vel'] >= gate]
    stats(sub, label)

# ── 2. Por dia da semana ───────────────────────────────────────────────────────
print("\n" + "=" * 75)
print("  2. BASE (sem gate) POR DIA DA SEMANA")
print("=" * 75)
print(f"  {'Dia':<30}  {'n':>3}  {'WR':>7}  {'STOP':>9}  {'PnL':>9}  {'avg':>8}")
print("  " + "-" * 65)
by_day = defaultdict(list)
for t in trades:
    by_day[t['dow']].append(t)
for dow in ['Thu', 'Fri', 'Sat', 'Sun', 'Mon']:
    grp = by_day.get(dow, [])
    if grp:
        stats(grp, f'{dow}')

# ── 3. Gate 0.13 por dia ───────────────────────────────────────────────────────
print("\n" + "=" * 75)
print("  3. GATE 0.13 POR DIA DA SEMANA")
print("=" * 75)
print(f"  {'Dia':<30}  {'n':>3}  {'WR':>7}  {'STOP':>9}  {'PnL':>9}  {'avg':>8}")
print("  " + "-" * 65)
for dow in ['Thu', 'Fri', 'Sat', 'Sun', 'Mon']:
    grp = [t for t in by_day.get(dow, []) if t['el_vel'] >= 0.13]
    if grp:
        stats(grp, f'{dow} + gate 0.13')
    elif by_day.get(dow):
        print(f"  {dow + ' + gate 0.13':<30}  n=  0  (nenhum trade passou o gate)")

# ── 4. Periodos comparados (43 vs 79) ─────────────────────────────────────────
print("\n" + "=" * 75)
print("  4. PERIODO: 1a amostra (Thu) vs restante (Fri-Mon)")
print("=" * 75)
print(f"  {'Periodo':<30}  {'n':>3}  {'WR':>7}  {'STOP':>9}  {'PnL':>9}  {'avg':>8}")
print("  " + "-" * 65)
fri = [t for t in trades if t['dow'] == 'Fri']
satmon = [t for t in trades if t['dow'] in ('Sat','Sun','Mon')]
stats(fri,    'Sexta (30 trades)')
stats(satmon, 'Sab-Seg (92 trades)')
stats(trades, 'Total (122 trades)')

# Gate 0.13 nos dois periodos
print()
fri13    = [t for t in fri    if t['el_vel'] >= 0.13]
satmon13 = [t for t in satmon if t['el_vel'] >= 0.13]
stats(fri13,    'Sexta + gate 0.13')
stats(satmon13, 'Sab-Seg + gate 0.13')

# ── 5. Resumo executivo ───────────────────────────────────────────────────────
print("\n" + "=" * 75)
print("  5. RESUMO EXECUTIVO — RANKING DOS CENARIOS")
print("=" * 75)
cenarios = []
for gate, label in [(0.08,'Base'),(0.10,'Gate 0.10'),(0.12,'Gate 0.12'),
                     (0.13,'Gate 0.13'),(0.15,'Gate 0.15')]:
    sub = [t for t in trades if t['el_vel'] >= gate]
    n = len(sub)
    if n == 0: continue
    lucro = sum(1 for t in sub if t['pnl'] > 0)
    stops = sum(1 for t in sub if t['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(t['pnl'] for t in sub), 2)
    cenarios.append((pnl, pnl/n, lucro/n, n, stops/n, label))

# Sexta isolada
sub = by_day.get('Fri', [])
if sub:
    n = len(sub)
    lucro = sum(1 for t in sub if t['pnl'] > 0)
    stops = sum(1 for t in sub if t['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(t['pnl'] for t in sub), 2)
    cenarios.append((pnl, pnl/n, lucro/n, n, stops/n, 'Sexta isolada'))

# Gate 0.13 + sexta
sub = [t for t in by_day.get('Fri', []) if t['el_vel'] >= 0.13]
if sub:
    n = len(sub)
    lucro = sum(1 for t in sub if t['pnl'] > 0)
    stops = sum(1 for t in sub if t['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(t['pnl'] for t in sub), 2)
    cenarios.append((pnl, pnl/n, lucro/n, n, stops/n, 'Sexta + gate 0.13'))

# Sat-Mon sem gate
sub = [t for t in trades if t['dow'] in ('Sat','Sun','Mon')]
if sub:
    n = len(sub)
    lucro = sum(1 for t in sub if t['pnl'] > 0)
    stops = sum(1 for t in sub if t['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(t['pnl'] for t in sub), 2)
    cenarios.append((pnl, pnl/n, lucro/n, n, stops/n, 'Sab-Seg sem gate'))

# Sat-Mon + gate 0.13
sub = [t for t in trades if t['dow'] in ('Sat','Sun','Mon') and t['el_vel'] >= 0.13]
if sub:
    n = len(sub)
    lucro = sum(1 for t in sub if t['pnl'] > 0)
    stops = sum(1 for t in sub if t['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(t['pnl'] for t in sub), 2)
    cenarios.append((pnl, pnl/n, lucro/n, n, stops/n, 'Sab-Seg + gate 0.13'))

print(f"  {'Cenario':<22}  {'n':>3}  {'WR':>6}  {'STOP%':>6}  {'PnL':>8}  {'avg':>7}  rank_avg")
print("  " + "-" * 70)
for rank, (pnl, avg, wr, n, stopr, label) in enumerate(
        sorted(cenarios, key=lambda x: x[1], reverse=True), 1):
    marker = ' <-- MELHOR avg/trade' if rank == 1 else ''
    print(f"  {label:<22}  {n:>3}  {wr:>6.1%}  {stopr:>6.1%}  {pnl:>+8.2f}  {avg:>+7.3f}  #{rank}{marker}")
