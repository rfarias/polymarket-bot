"""
Simula v2 EE (stop 0.65 + profit protect 0.88 @ secs<=70) nos trades reais locais.
Usa os mesmos entry points do runner v1 (ee_paper_entry events) e replaya
os snapshots com a lógica v2 tick a tick.
"""
import sys, io, json
from pathlib import Path
from collections import defaultdict, Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

QTY                    = 6.0
EE_STOP_LEVEL          = 0.65
EE_PROFIT_PROTECT_BID  = 0.88
EE_PROFIT_PROTECT_SECS = 70

sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

slug_snaps  = defaultdict(list)
entry_info  = {}   # slug -> {ep, secs, side, el_vel, sess}
real_closed = {}   # slug -> {outcome, pnl}

for lp in sessions:
    sname = lp.parent.name
    for line in lp.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        t    = e.get('type', '')
        slug = e.get('slug') or e.get('current_slug') or ''
        if not slug:
            continue

        if t == 'snapshot':
            el    = e.get('early_leader') or {}
            exec_ = e.get('current_exec') or {}
            slug_snaps[slug].append({
                'secs':    e.get('current_secs'),
                'up_bid':  exec_.get('up_bid', 0),
                'dn_bid':  exec_.get('down_bid', 0),
                'el_side': el.get('early_side'),
            })

        elif t == 'ee_paper_entry':
            el = e.get('el', {})
            entry_info[slug] = {
                'ep':     float(e.get('ep') or 0),
                'secs':   e.get('secs'),
                'side':   e.get('side', ''),
                'el_vel': float(el.get('el_vel') or 0),
                'sess':   sname[-6:],
            }

        elif t == 'ee_paper_closed':
            ee = e.get('ee', {})
            outcome = ee.get('outcome', '')
            if outcome and outcome != 'MISSED':
                real_closed[slug] = {
                    'outcome': outcome,
                    'pnl':     float(ee.get('pnl') or 0),
                }


def _el_bid(snap, side):
    return snap.get('up_bid', 0) if side == 'UP' else snap.get('dn_bid', 0)

def _opp_bid(snap, side):
    return snap.get('dn_bid', 0) if side == 'UP' else snap.get('up_bid', 0)


def simulate_v2(snaps_all, entry_secs, ep, side):
    """Replay snapshots a partir do momento de entrada com lógica v2."""
    # Ordena por secs decrescente = cronológico (maior secs = mais tempo restante = mais cedo)
    snaps_sorted = sorted(snaps_all, key=lambda s: s.get('secs') or 0, reverse=True)

    # Hold = snapshots do momento de entrada em diante (secs <= entry_secs)
    hold = [s for s in snaps_sorted if (s.get('secs') or 9999) <= entry_secs]

    for s in hold:
        secs = s.get('secs') or 999
        el   = _el_bid(s, side)
        opp  = _opp_bid(s, side)

        # 1) Resolução no final
        if secs <= 35:
            if el >= 0.85:
                return 'WIN', 1.0, round((1.0 - ep) * QTY, 4)
            elif opp >= 0.85:
                return 'REVERSAL', 0.0, round((0.0 - ep) * QTY, 4)

        # 2) Profit protect
        if 36 <= secs <= EE_PROFIT_PROTECT_SECS and el >= EE_PROFIT_PROTECT_BID:
            return 'PROFIT_PROTECT', el, round((el - ep) * QTY, 4)

        # 3) Stop loss
        if el > 0 and el < EE_STOP_LEVEL:
            return 'STOP_LOSS', EE_STOP_LEVEL, round((EE_STOP_LEVEL - ep) * QTY, 4)

        # 4) Hedge fallback (bid crashed abaixo de 0.50)
        if el < 0.50 and opp > 0:
            pnl_hedge = round(((1.0 - opp) - ep) * QTY, 4)
            return 'WIN_HEDGE', opp, pnl_hedge

    return 'UNKNOWN', 0.0, 0.0


# ── Simular todos os trades com entry real ────────────────────────────────────
results = []
for slug, entry in entry_info.items():
    real = real_closed.get(slug)
    if not real:
        continue   # MISSED ou ainda aberto

    snaps = slug_snaps.get(slug, [])
    if not snaps:
        continue

    v2_out, v2_exit, v2_pnl = simulate_v2(snaps, entry['secs'], entry['ep'], entry['side'])

    results.append({
        'slug':       slug[-6:],
        'ep':         entry['ep'],
        'entry_secs': entry['secs'],
        'el_vel':     entry['el_vel'],
        'sess':       entry['sess'],
        'v1_outcome': real['outcome'],
        'v1_pnl':     real['pnl'],
        'v2_outcome': v2_out,
        'v2_exit':    round(v2_exit, 4),
        'v2_pnl':     v2_pnl,
        'delta':      round(v2_pnl - real['pnl'], 4),
    })

results.sort(key=lambda r: (r['ep'], r['entry_secs'] or 0))

# ── Saída ─────────────────────────────────────────────────────────────────────
n = len(results)
print(f"\n{'='*80}")
print(f"  SIMULAÇÃO v2 (stop={EE_STOP_LEVEL}, PP bid>={EE_PROFIT_PROTECT_BID} @ secs<={EE_PROFIT_PROTECT_SECS})")
print(f"  {n} trades com entry real  |  QTY={QTY}")
print(f"{'='*80}\n")

print(f"{'Slug':>8}  {'EP':>6}  {'Secs':>5}  {'elVel':>6}  "
      f"{'V1':>14}  {'V1 PnL':>8}  {'V2':>14}  {'V2 PnL':>8}  {'Δ':>7}")
print('-' * 96)
for r in results:
    changed = '  <--' if r['v1_outcome'] != r['v2_outcome'] else ''
    print(f"{r['slug']:>8}  {r['ep']:>6.3f}  {str(r['entry_secs']):>5}  {r['el_vel']:>6.3f}  "
          f"{r['v1_outcome']:>14}  {r['v1_pnl']:>+8.4f}  "
          f"{r['v2_outcome']:>14}  {r['v2_pnl']:>+8.4f}  {r['delta']:>+7.4f}{changed}")
print('-' * 96)

v1_total = sum(r['v1_pnl'] for r in results)
v2_total = sum(r['v2_pnl'] for r in results)
v1_wins  = sum(1 for r in results if r['v1_outcome'] in ('WIN', 'PROFIT_PROTECT'))
v2_wins  = sum(1 for r in results if r['v2_outcome'] in ('WIN', 'PROFIT_PROTECT'))

print(f"\n  v1 (real):  WR={v1_wins}/{n}={v1_wins/n*100:.0f}%  PnL={v1_total:+.4f}  avg={v1_total/n:+.4f}")
print(f"  v2 (sim):   WR={v2_wins}/{n}={v2_wins/n*100:.0f}%  PnL={v2_total:+.4f}  avg={v2_total/n:+.4f}")
print(f"  Delta v2−v1: {v2_total-v1_total:+.4f}\n")

print("Outcomes v2 simulado:")
c2 = Counter(r['v2_outcome'] for r in results)
for k, n_k in c2.most_common():
    p = sum(r['v2_pnl'] for r in results if r['v2_outcome'] == k)
    print(f"  {k:<16} {n_k:>3}   PnL={p:+.4f}")

# Trades com divergência v1 != v2
changed = [r for r in results if r['v1_outcome'] != r['v2_outcome']]
if changed:
    print(f"\nDivergências v1→v2 ({len(changed)} trades):")
    for r in changed:
        print(f"  {r['slug']}  ep={r['ep']:.3f}  secs={r['entry_secs']}  "
              f"v1={r['v1_outcome']}({r['v1_pnl']:+.4f})  "
              f"v2={r['v2_outcome']}({r['v2_pnl']:+.4f})  Δ={r['delta']:+.4f}")

# Por faixa de EP
print(f"\nPnL por faixa EP — v2 simulado:")
for lo, hi in [(0.82,0.83),(0.83,0.84),(0.84,0.85),(0.85,0.86),(0.86,0.87)]:
    grp = [r for r in results if lo <= r['ep'] < hi]
    if not grp:
        continue
    p2 = sum(r['v2_pnl'] for r in grp)
    p1 = sum(r['v1_pnl'] for r in grp)
    w2 = sum(1 for r in grp if r['v2_outcome'] in ('WIN','PROFIT_PROTECT'))
    print(f"  [{lo:.2f},{hi:.2f})  n={len(grp):>2}  WR={w2/len(grp)*100:.0f}%  "
          f"PnL_v2={p2:+.4f}  PnL_v1={p1:+.4f}  Δ={p2-p1:+.4f}")
