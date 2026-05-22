"""
Analisa:
1. Distribuição de secs no momento da entrada
2. Janela de bid quando signal_ok=True — há bids abaixo de 0.82 disponíveis?
"""
import json
from pathlib import Path
from collections import defaultdict

sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

entries = {}   # slug -> entry event
snaps_by_slug = defaultdict(list)

for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
    for r in rows:
        t = r.get('type')
        if t == 'ee_paper_entry':
            entries[r['slug']] = r
        elif t == 'snapshot':
            slug = r.get('current_slug', '')
            if not slug:
                continue
            el = r.get('early_leader', {}) or {}
            if el.get('signal_ok'):
                snaps_by_slug[slug].append({
                    'secs':   r.get('current_secs'),
                    'up_bid': (r.get('current_exec') or {}).get('up_bid', 0),
                    'dn_bid': (r.get('current_exec') or {}).get('down_bid', 0),
                    'el_side': el.get('early_side'),
                    'el_vel':  el.get('el_vel', 0),
                })

# --- 1. Distribuição de secs ---
print('=== 1. DISTRIBUIÇÃO DE secs NA ENTRADA ===\n')
secs_vals = [e.get('secs', 0) for e in entries.values()]
secs_vals.sort(reverse=True)

buckets = [(180, 160), (159, 140), (139, 120), (119, 100), (99, 80), (79, 60), (59, 30)]
print(f"  {'Janela (secs)':>16}  {'n':>4}  {'%':>6}")
print('  ' + '-' * 30)
for hi, lo in buckets:
    n = sum(1 for s in secs_vals if lo <= s <= hi)
    pct = n / len(secs_vals) * 100 if secs_vals else 0
    mins_hi = (300 - hi) // 60
    secs_hi = (300 - hi) % 60
    mins_lo = (300 - lo) // 60
    secs_lo = (300 - lo) % 60
    label = f"[{lo}–{hi}]"
    wall = f"~{mins_hi}:{secs_hi:02d}–{mins_lo}:{secs_lo:02d} do início"
    print(f"  {label:>16}  {n:>4}  {pct:>5.0f}%  {wall}")

print(f"\n  secs range: {min(secs_vals)} – {max(secs_vals)}  |  média: {sum(secs_vals)/len(secs_vals):.0f}s restantes")
print(f"  = entrada entre {(300-max(secs_vals))//60}:{(300-max(secs_vals))%60:02d} e {(300-min(secs_vals))//60}:{(300-min(secs_vals))%60:02d} do início do candle\n")

# --- 2. Bid disponível quando signal_ok ---
print('=== 2. BID EL DISPONÍVEL QUANDO signal_ok=True ===\n')
print('  (snaps com signal_ok ativo, agrupados por faixa de bid EL)\n')

all_signal_snaps = []
for slug, snaps in snaps_by_slug.items():
    entry = entries.get(slug)
    entry_secs = entry.get('secs', 0) if entry else None
    for s in snaps:
        el_side = s['el_side']
        el_bid = s['up_bid'] if el_side == 'UP' else s['dn_bid']
        # Só snaps ANTES da entrada (ou sem entrada)
        if entry_secs is None or s['secs'] > entry_secs:
            all_signal_snaps.append({
                'slug': slug,
                'secs': s['secs'],
                'el_bid': el_bid,
                'entered': entry is not None,
            })

bid_buckets = [
    (0.60, 0.70), (0.70, 0.74), (0.74, 0.78),
    (0.78, 0.80), (0.80, 0.82), (0.82, 0.84),
    (0.84, 0.86), (0.86, 0.90), (0.90, 1.01),
]
print(f"  {'Faixa bid EL':>16}  {'n snaps':>8}  {'n slugs':>8}  {'nota':>20}")
print('  ' + '-' * 60)
for lo, hi in bid_buckets:
    sn = [s for s in all_signal_snaps if lo <= s['el_bid'] < hi]
    slugs_uniq = len(set(s['slug'] for s in sn))
    nota = ''
    if lo >= 0.82:
        nota = '<-- faixa atual de entrada'
    elif lo >= 0.70:
        nota = '<-- abaixo do atual'
    elif lo >= 0.60:
        nota = '<-- zona de risco'
    print(f"  [{lo:.2f} – {hi:.2f})  {len(sn):>8}  {slugs_uniq:>8}  {nota}")

print(f"\n  Total snaps com signal_ok antes da entrada: {len(all_signal_snaps)}")
print(f"  Slugs distintos: {len(set(s['slug'] for s in all_signal_snaps))}")

# --- 3. Simular entrada mais baixa ---
print('\n=== 3. SIMULAÇÃO: e se entrássemos também em 0.70–0.81? ===\n')
print('  (usando o primeiro snap disponível com signal_ok em cada slug)')
print('  Nota: PnL simulado assume WIN=0.99, STOP=0.65, sem dados reais do outcome\n')

# Para cada slug com entrada real, qual era o bid mais cedo com signal_ok?
print(f"  {'Slug':>8}  {'entry EP':>9}  {'1o signal bid':>14}  {'secs (signal)':>14}  {'outcome':>12}  {'PnL atual':>10}")
print('  ' + '-' * 75)
for slug, entry in sorted(entries.items()):
    snaps = snaps_by_slug.get(slug, [])
    if not snaps:
        continue
    snaps_sorted = sorted(snaps, key=lambda s: s['secs'], reverse=True)  # maior secs = mais cedo
    first = snaps_sorted[0]
    el_side = first['el_side']
    el_bid_first = first['up_bid'] if el_side == 'UP' else first['dn_bid']

    # buscar outcome
    from pathlib import Path as P
    closed_pnl = '?'
    outcome = '?'
    for lp in sessions:
        rows2 = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
        cl = next((r for r in rows2 if r.get('type') == 'ee_paper_closed' and r.get('slug') == slug), None)
        if cl:
            outcome = cl['ee']['outcome']
            closed_pnl = f'{cl["ee"]["pnl"]:+.2f}'
            break

    ep = entry.get('ep', 0)
    print(f"  {slug[-6:]:>8}  {ep:>9.3f}  {el_bid_first:>14.3f}  {first['secs']:>14}  {outcome:>12}  {closed_pnl:>10}")
