"""
Simula subir o teto de entrada de 0.86 para 0.90 e 0.92.
Lógica correta: entra no 1º snap onde signal_ok=True E bid está em [0.82, ceiling].
Stop fixo 0.65 + PP 0.88/secs<=70 (conforme runner v2).
Compara com a estratégia atual (teto 0.86).
"""
from __future__ import annotations
import json
from pathlib import Path
from collections import defaultdict

QTY     = 6.0
FLOOR   = 0.82
STOP    = 0.65
PP_BID  = 0.88
PP_SECS = 70

sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

slug_snaps:  dict[str, list[dict]] = defaultdict(list)
slug_entry:  dict[str, dict]       = {}

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


def _el(s, side):
    return s.get('up_bid', 0) if side == 'UP' else s.get('dn_bid', 0)

def _opp(s, side):
    return s.get('dn_bid', 0) if side == 'UP' else s.get('up_bid', 0)


def simulate(snaps_sorted, entry_idx, ep, el_side):
    """Simula com stop 0.65 + PP."""
    for s in snaps_sorted[entry_idx:]:
        secs = s.get('secs') or 999
        el   = _el(s, el_side)
        opp  = _opp(s, el_side)
        if secs <= 35:
            if el >= 0.85:
                return 'WIN', 1.0
            elif opp >= 0.85:
                return 'LOSS', 0.0
        if 36 <= secs <= PP_SECS and el >= PP_BID:
            return 'PP', el
        if 0 < el < STOP:
            return 'STOP', STOP
        if el < 0.50 and opp > 0:
            return 'HEDGE', opp
    return 'UNKNOWN', 0.0


def find_entry(snaps_sorted, ceiling):
    """Retorna (idx, ep, el_side) do 1º snap com signal_ok e bid em [FLOOR, ceiling]."""
    for i, s in enumerate(snaps_sorted):
        if not s.get('signal_ok'):
            continue
        side = s.get('el_side')
        if not side:
            continue
        bid = _el(s, side)
        if FLOOR <= bid <= ceiling:
            return i, round(bid, 4), side
    return None, None, None


def run_ceiling(ceiling):
    trades = []
    for slug, snaps in slug_snaps.items():
        ss = sorted(snaps, key=lambda x: x.get('secs') or 0, reverse=True)
        idx, ep, side = find_entry(ss, ceiling)
        if idx is None:
            continue
        el_vel = ss[idx].get('el_vel', 0)
        outcome, exit_ep = simulate(ss, idx, ep, side)
        pnl = round((exit_ep - ep) * QTY if outcome != 'LOSS' else (0.0 - ep) * QTY, 2)
        trades.append({
            'slug':    slug[-6:],
            'ep':      ep,
            'el_vel':  el_vel,
            'outcome': outcome,
            'pnl':     pnl,
            'is_real': slug in slug_entry,
            'real_ep': slug_entry.get(slug, {}).get('ep'),
        })
    return trades


# ── Resultados ────────────────────────────────────────────────────────────────
print(f"\n{'='*78}")
print(f"  SIMULAÇÃO TETO DE ENTRADA — floor={FLOOR}  stop={STOP}  PP>={PP_BID}@secs<={PP_SECS}")
print(f"{'='*78}\n")

for ceiling in [0.86, 0.90, 0.92]:
    trades = run_ceiling(ceiling)
    n      = len(trades)
    wins   = [t for t in trades if t['outcome'] in ('WIN', 'PP')]
    stops  = [t for t in trades if t['outcome'] == 'STOP']
    losses = [t for t in trades if t['outcome'] in ('LOSS', 'HEDGE')]
    pnl    = round(sum(t['pnl'] for t in trades), 2)
    wr     = len(wins) / n if n else 0
    avg    = pnl / n if n else 0

    new_trades = [t for t in trades if not t['is_real']]
    changed    = [t for t in trades if t['is_real'] and t['real_ep'] and abs(t['ep'] - t['real_ep']) > 0.005]

    print(f"─── Teto {ceiling} ── {n} trades ({len(new_trades)} novos, {len(changed)} com ep alterado) ───")
    print(f"  WIN+PP: {len(wins)}  STOP: {len(stops)}  LOSS: {len(losses)}  "
          f"WR: {wr:.1%}  PnL: {pnl:>+.2f}  avg: {avg:>+.3f}\n")

    # Tabela trade a trade
    print(f"  {'Slug':>8}  {'EP':>6}  {'EP_real':>8}  {'Δep':>6}  {'el_vel':>7}  "
          f"{'Outcome':>8}  {'PnL':>7}  {'Novo?':>6}")
    print("  " + "-" * 66)
    for t in sorted(trades, key=lambda x: x['slug']):
        dep     = round(t['ep'] - t['real_ep'], 3) if t['real_ep'] else None
        dep_str = f"{dep:>+.3f}" if dep is not None else "  novo"
        novo    = "SIM" if not t['is_real'] else "-"
        real_str = f"{t['real_ep']:.3f}" if t['real_ep'] else "  —"
        print(f"  {t['slug']:>8}  {t['ep']:>6.3f}  {real_str:>8}  {dep_str:>6}  "
              f"{t['el_vel']:>7.3f}  {t['outcome']:>8}  {t['pnl']:>+7.2f}  {novo:>6}")
    print()

# ── Resumo comparativo ────────────────────────────────────────────────────────
print(f"\n{'='*78}")
print(f"  RESUMO COMPARATIVO")
print(f"{'='*78}\n")
print(f"  {'Teto':>6}  {'Trades':>7}  {'Novos':>6}  {'WR':>7}  {'PnL':>9}  {'avg/trade':>10}")
print("  " + "-" * 52)
for ceiling in [0.86, 0.90, 0.92]:
    trades = run_ceiling(ceiling)
    n    = len(trades)
    new_ = sum(1 for t in trades if not t['is_real'])
    wins = sum(1 for t in trades if t['outcome'] in ('WIN', 'PP'))
    pnl  = round(sum(t['pnl'] for t in trades), 2)
    print(f"  {ceiling:>6.2f}  {n:>7}  {new_:>6}  {wins/n:>7.1%}  {pnl:>+9.2f}  {pnl/n:>+10.3f}")

# ── Apenas os novos trades (teto 0.90 e 0.92) ─────────────────────────────────
print(f"\n{'='*78}")
print(f"  TRADES NOVOS CAPTURADOS — detalhe por teto")
print(f"{'='*78}\n")
for ceiling in [0.90, 0.92]:
    t86   = run_ceiling(0.86)
    slugs_86 = {t['slug'] for t in t86}
    tall  = run_ceiling(ceiling)
    novos = [t for t in tall if t['slug'] not in slugs_86]
    if not novos:
        print(f"  Teto {ceiling}: nenhum trade novo além do teto 0.86\n")
        continue
    wins = sum(1 for t in novos if t['outcome'] in ('WIN', 'PP'))
    pnl  = round(sum(t['pnl'] for t in novos), 2)
    print(f"  Teto {ceiling} — {len(novos)} trades novos  WR:{wins/len(novos):.0%}  PnL:{pnl:>+.2f}")
    print(f"  {'Slug':>8}  {'EP':>6}  {'el_vel':>7}  {'Outcome':>8}  {'PnL':>7}")
    print("  " + "-" * 44)
    for t in sorted(novos, key=lambda x: x['slug']):
        print(f"  {t['slug']:>8}  {t['ep']:>6.3f}  {t['el_vel']:>7.3f}  {t['outcome']:>8}  {t['pnl']:>+7.2f}")
    print()
