"""
Para os trades novos (signal_ok mas bid < 0.82), verifica:
Sem nenhum stop — apenas hold to resolution — o lado apostado venceu?
"""
import json
from pathlib import Path
from collections import defaultdict

QTY = 6.0
sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

slug_snaps:  dict[str, list[dict]] = defaultdict(list)
slug_entry:  dict[str, dict]       = {}
slug_closed: dict[str, dict]       = {}

for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
    for r in rows:
        t    = r.get('type')
        slug = r.get('slug') or r.get('current_slug') or ''
        if not slug:
            continue
        if t == 'snapshot':
            el    = r.get('early_leader') or {}
            exec_ = r.get('current_exec') or {}
            slug_snaps[slug].append({
                'secs':      r.get('current_secs'),
                'up_bid':    exec_.get('up_bid', 0),
                'dn_bid':    exec_.get('down_bid', 0),
                'el_side':   el.get('early_side'),
                'signal_ok': el.get('signal_ok', False),
                'el_vel':    el.get('el_vel', 0),
            })
        elif t == 'ee_paper_entry':
            slug_entry[slug] = r
        elif t == 'ee_paper_closed':
            ee = r.get('ee') or {}
            if ee.get('outcome') and ee['outcome'] != 'MISSED':
                slug_closed[slug] = ee


def _el_bid(s, side):
    return s.get('up_bid', 0) if side == 'UP' else s.get('dn_bid', 0)

def _opp_bid(s, side):
    return s.get('dn_bid', 0) if side == 'UP' else s.get('up_bid', 0)

def _resolve_direction(snaps_sorted, first_idx, el_side):
    """Olha os últimos snaps do slug para ver qual lado resolveu."""
    hold = snaps_sorted[first_idx:]
    # Procura snap com secs <= 10 onde um lado está >= 0.95
    for s in hold:
        secs = s.get('secs') or 999
        el   = _el_bid(s, el_side)
        opp  = _opp_bid(s, el_side)
        if secs <= 15:
            if el >= 0.90:
                return 'EL_WIN', el
            elif opp >= 0.90:
                return 'EL_LOSS', opp
    # Se não achou resolução clara, pega o último snap
    last = hold[-1] if hold else None
    if last:
        el  = _el_bid(last, el_side)
        opp = _opp_bid(last, el_side)
        if el > opp:
            return 'EL_WIN?', el
        else:
            return 'EL_LOSS?', opp
    return 'UNKNOWN', 0.0


# Slugs sem entrada real (novos trades potenciais)
new_slugs = [s for s in slug_snaps if s not in slug_entry]

results = []
for slug in new_slugs:
    snaps = sorted(slug_snaps[slug], key=lambda x: x.get('secs') or 0, reverse=True)
    # Acha 1º signal_ok
    first_idx = next((i for i, s in enumerate(snaps) if s.get('signal_ok') and _el_bid(s, s.get('el_side') or 'UP') > 0), None)
    if first_idx is None:
        continue
    entry_snap = snaps[first_idx]
    el_side    = entry_snap.get('el_side')
    if not el_side:
        continue
    ep         = round(_el_bid(entry_snap, el_side), 3)
    entry_secs = entry_snap.get('secs')
    el_vel     = entry_snap.get('el_vel', 0)

    direction, final_bid = _resolve_direction(snaps, first_idx, el_side)
    el_won = direction.startswith('EL_WIN')

    # PnL se segurou até a resolução (sem stop)
    pnl_hold = round((1.0 - ep) * QTY if el_won else (0.0 - ep) * QTY, 2)

    # min_bid durante hold
    hold      = snaps[first_idx:]
    el_bids   = [_el_bid(s, el_side) for s in hold if _el_bid(s, el_side) > 0]
    min_bid   = round(min(el_bids), 3) if el_bids else None

    results.append({
        'slug':        slug[-6:],
        'el_side':     el_side,
        'ep':          ep,
        'entry_secs':  entry_secs,
        'el_vel':      el_vel,
        'direction':   direction,
        'el_won':      el_won,
        'pnl_hold':    pnl_hold,
        'min_bid':     min_bid,
    })

results.sort(key=lambda x: x['slug'])

wins   = [r for r in results if r['el_won']]
losses = [r for r in results if not r['el_won']]

print(f"\n{'='*80}")
print(f"  RESULTADO DIRECIONAL — hold to resolution, sem stop")
print(f"  (apenas slugs com signal_ok mas bid < 0.82 na 1ª detecção)")
print(f"{'='*80}\n")

print(f"{'Slug':>8}  {'Side':>4}  {'EP':>6}  {'secs':>5}  {'el_vel':>7}  {'Direção':>10}  {'PnL hold':>9}  {'min_bid':>8}")
print('-' * 72)
for r in results:
    won_str = 'EL GANHOU' if r['el_won'] else 'EL PERDEU'
    print(f"{r['slug']:>8}  {r['el_side']:>4}  {r['ep']:>6.3f}  "
          f"{str(r['entry_secs']):>5}  {r['el_vel']:>7.3f}  {won_str:>10}  "
          f"{r['pnl_hold']:>+9.2f}  {str(r['min_bid']):>8}")

print('-' * 72)
n   = len(results)
nw  = len(wins)
pnl = round(sum(r['pnl_hold'] for r in results), 2)
print(f"\n  Total: {n}  |  EL ganhou: {nw} ({nw/n:.0%})  |  EL perdeu: {n-nw} ({(n-nw)/n:.0%})")
print(f"  PnL total (hold, sem stop): {pnl:>+.2f}")
print(f"  avg/trade: {pnl/n:>+.2f}\n")

# Separar por faixa de EP
print(f"  Resultado por faixa de EP (sem stop):")
for lo, hi in [(0.60,0.75),(0.75,0.80),(0.80,0.82)]:
    sub = [r for r in results if lo <= r['ep'] < hi]
    if not sub:
        continue
    sw = sum(1 for r in sub if r['el_won'])
    sp = round(sum(r['pnl_hold'] for r in sub), 2)
    print(f"    [{lo:.2f}–{hi:.2f})  n={len(sub)}  EL ganhou={sw} ({sw/len(sub):.0%})  PnL={sp:>+.2f}")
