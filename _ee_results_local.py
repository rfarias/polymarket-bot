"""Resultados do EE paper runner nos logs locais."""
import sys, io, json
from pathlib import Path
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

QTY = 6.0
sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

trades = []
for lp in sessions:
    for line in lp.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get('type') == 'ee_paper_closed':
            ee = e.get('ee', {})
            trades.append({
                'slug':        e.get('slug', ''),
                'side':        ee.get('entry_side', ''),
                'entry_ep':    float(ee.get('entry_ep') or 0),
                'entry_secs':  ee.get('entry_secs'),
                'hedge_ep':    ee.get('hedge_ep'),
                'outcome':     ee.get('outcome', ''),
                'pnl':         float(ee.get('pnl') or 0),
                'qty':         float(ee.get('qty') or QTY),
                'sess':        lp.parent.name[-6:],
            })

# ── Estatísticas por outcome ──────────────────────────────────────────────────
from collections import Counter
outcomes = Counter(t['outcome'] for t in trades)
pnl_total = sum(t['pnl'] for t in trades)
wins   = [t for t in trades if 'WIN' in t['outcome'] and 'HEDGE' not in t['outcome'] and 'BOUNCE' not in t['outcome']]
hedges = [t for t in trades if 'HEDGE' in t['outcome']]
losses = [t for t in trades if t['outcome'] in ('REVERSAL', 'STOP_LOSS')]
pp     = [t for t in trades if t['outcome'] == 'PROFIT_PROTECT']

print(f"=== EE PAPER — RESULTADOS LOCAIS (logs reais deste PC) ===")
print(f"Sessoes: {len(sessions)}   |   Trades: {len(trades)}")
print(f"Primeira: {sessions[0].parent.name}   Ultima: {sessions[-1].parent.name}")
print()
print("Outcomes:")
for k, v in outcomes.most_common():
    pnl_k = sum(t['pnl'] for t in trades if t['outcome'] == k)
    print(f"  {k:<18} {v:>3} trades   PnL={pnl_k:+.4f}")
print()
print(f"WR (WIN only): {len(wins)}/{len(trades)} = {len(wins)/len(trades)*100:.1f}%")
print(f"PnL total: {pnl_total:+.4f}")
if trades:
    print(f"avg/trade:  {pnl_total/len(trades):+.4f}")
print()

# ── Trades detalhados ─────────────────────────────────────────────────────────
print("--- Todos os trades em ordem ---")
for t in trades:
    slug_s   = str(t['slug'])[-15:]
    hedge_m  = f"  hedge@{t['hedge_ep']:.2f}" if t['hedge_ep'] else ''
    print(f"  [{t['outcome']:<16}] {t['sess']} {t['side']} ep={t['entry_ep']:.2f} secs={t['entry_secs']}  PnL={t['pnl']:+.4f}{hedge_m}  ...{slug_s}")

print()
# ── Análise por faixa de entry_price ─────────────────────────────────────────
print("--- PnL por faixa de entry price ---")
bands = [(0.82, 0.83), (0.83, 0.84), (0.84, 0.85), (0.85, 0.86), (0.86, 0.87)]
for lo, hi in bands:
    grp = [t for t in trades if lo <= t['entry_ep'] < hi]
    if grp:
        pnl_g = sum(t['pnl'] for t in grp)
        wr_g  = sum(1 for t in grp if 'WIN' in t['outcome']) / len(grp)
        print(f"  ep [{lo:.2f},{hi:.2f}): n={len(grp):>2}  WR={wr_g*100:.0f}%  PnL={pnl_g:+.4f}")

print()
# ── el_vel dos hedges vs wins ─────────────────────────────────────────────────
print("--- el_vel: WIN_HEDGE vs WIN (via logs de snapshot) ---")
slug_vel = {}
for lp in sessions:
    for line in lp.read_text(encoding='utf-8').splitlines():
        if not line.strip(): continue
        try: e = json.loads(line)
        except: continue
        if e.get('type') == 'snapshot':
            slug = e.get('slug') or e.get('current_slug') or ''
            el   = e.get('early_leader') or {}
            vel  = el.get('el_vel')
            if slug and vel is not None and slug not in slug_vel:
                slug_vel[slug] = float(vel)

for t in trades:
    slug = t['slug']
    vel  = slug_vel.get(slug)
    t['el_vel'] = vel

wins_vel  = [t['el_vel'] for t in trades if 'HEDGE' not in t['outcome'] and 'REVERSAL' not in t['outcome'] and t['el_vel'] is not None]
hedge_vel = [t['el_vel'] for t in trades if 'HEDGE' in t['outcome'] and t['el_vel'] is not None]

if wins_vel:
    print(f"  WIN avg el_vel:      {sum(wins_vel)/len(wins_vel):.4f}")
if hedge_vel:
    print(f"  WIN_HEDGE avg el_vel: {sum(hedge_vel)/len(hedge_vel):.4f}")
