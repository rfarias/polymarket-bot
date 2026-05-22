"""
Simula entrada no 1º signal_ok (qualquer bid) com diferentes estratégias de stop:
  A) Stop fixo em vários níveis (0.60, 0.55, 0.50, ...)
  B) Stop dinâmico = entry - distância (ex: entry - 0.10, entry - 0.15, ...)

Para cada trade, reproduz a trajetória tick a tick dos snapshots.
"""
from __future__ import annotations
import json
from pathlib import Path
from collections import defaultdict

QTY      = 6.0
PP_BID   = 0.88
PP_SECS  = 70

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


def _el_bid(s: dict, side: str) -> float:
    return s.get('up_bid', 0) if side == 'UP' else s.get('dn_bid', 0)

def _opp_bid(s: dict, side: str) -> float:
    return s.get('dn_bid', 0) if side == 'UP' else s.get('up_bid', 0)


def simulate_trade(snaps_sorted: list[dict], entry_idx: int, ep: float,
                   el_side: str, stop_level: float) -> dict:
    """
    Simula hold from entry_idx com stop fixo.
    Retorna: outcome, exit_ep, pnl
    """
    hold = snaps_sorted[entry_idx:]
    for s in hold:
        secs = s.get('secs') or 999
        el   = _el_bid(s, el_side)
        opp  = _opp_bid(s, el_side)

        if secs <= 35:
            if el >= 0.85:
                return {'outcome': 'WIN',    'exit': 1.0,  'pnl': round((1.0 - ep) * QTY, 2)}
            elif opp >= 0.85:
                return {'outcome': 'LOSS',   'exit': 0.0,  'pnl': round((0.0 - ep) * QTY, 2)}
        if 36 <= secs <= PP_SECS and el >= PP_BID:
            return {'outcome': 'PP',     'exit': el,   'pnl': round((el - ep) * QTY, 2)}
        if el > 0 and el < stop_level:
            return {'outcome': 'STOP',   'exit': stop_level, 'pnl': round((stop_level - ep) * QTY, 2)}
        if el < 0.50 and opp > 0:
            # hedge fallback (bid saltou o stop)
            return {'outcome': 'HEDGE',  'exit': opp,  'pnl': round(((1.0 - opp) - ep) * QTY, 2)}

    return {'outcome': 'UNKNOWN', 'exit': 0.0, 'pnl': 0.0}


# Monta lista de trades: todos os slugs com signal_ok
trades_base = []
for slug, snaps in slug_snaps.items():
    snaps_sorted = sorted(snaps, key=lambda x: x.get('secs') or 0, reverse=True)
    first_idx = next(
        (i for i, s in enumerate(snaps_sorted)
         if s.get('signal_ok') and _el_bid(s, s.get('el_side') or 'UP') > 0),
        None
    )
    if first_idx is None:
        continue
    s0      = snaps_sorted[first_idx]
    el_side = s0.get('el_side')
    if not el_side:
        continue
    ep      = round(_el_bid(s0, el_side), 4)
    el_vel  = s0.get('el_vel', 0)
    has_real_entry = slug in slug_entry
    real_ep = slug_entry[slug].get('ep') if has_real_entry else None

    trades_base.append({
        'slug':      slug[-6:],
        'slug_full': slug,
        'el_side':   el_side,
        'ep':        ep,
        'real_ep':   real_ep,
        'el_vel':    el_vel,
        'snaps':     snaps_sorted,
        'idx':       first_idx,
        'is_new':    not has_real_entry,
    })

n_total   = len(trades_base)
n_new     = sum(1 for t in trades_base if t['is_new'])
n_existing = n_total - n_new

print(f"\n{'='*78}")
print(f"  SIMULAÇÃO STOP — entry no 1º signal_ok ({n_existing} existentes + {n_new} novos = {n_total} total)")
print(f"{'='*78}\n")

# ── A) Stop fixo ──────────────────────────────────────────────────────────────
print("A) STOP FIXO (mesmo nível para todas as entradas)\n")
print(f"  {'Stop':>8}  {'Trades':>7}  {'WIN+PP':>7}  {'STOP':>6}  {'LOSS':>6}  {'WR':>6}  {'PnL':>9}  {'avg':>7}")
print("  " + "-" * 65)

fixed_stops = [0.70, 0.67, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40]
for sl in fixed_stops:
    wins = stops = losses = 0
    pnl_total = 0.0
    for t in trades_base:
        res = simulate_trade(t['snaps'], t['idx'], t['ep'], t['el_side'], sl)
        pnl_total += res['pnl']
        if res['outcome'] in ('WIN', 'PP'):
            wins += 1
        elif res['outcome'] == 'STOP':
            stops += 1
        else:
            losses += 1
    wr  = wins / n_total
    avg = pnl_total / n_total
    print(f"  {sl:>8.2f}  {n_total:>7}  {wins:>7}  {stops:>6}  {losses:>6}  "
          f"{wr:>6.1%}  {pnl_total:>+9.2f}  {avg:>+7.3f}")

# referência: sem stop (apenas PP)
wins = losses = 0
pnl_ns = 0.0
for t in trades_base:
    res = simulate_trade(t['snaps'], t['idx'], t['ep'], t['el_side'], stop_level=0.0)
    pnl_ns += res['pnl']
    if res['outcome'] in ('WIN', 'PP'):
        wins += 1
    else:
        losses += 1
print(f"  {'sem stop':>8}  {n_total:>7}  {wins:>7}  {'0':>6}  {losses:>6}  "
      f"{wins/n_total:>6.1%}  {pnl_ns:>+9.2f}  {pnl_ns/n_total:>+7.3f}  <- referencia")

# ── B) Stop dinâmico = entry - distância ──────────────────────────────────────
print(f"\nB) STOP DINAMICO = entry - distancia\n")
print(f"  {'Dist':>8}  {'Trades':>7}  {'WIN+PP':>7}  {'STOP':>6}  {'LOSS':>6}  {'WR':>6}  {'PnL':>9}  {'avg':>7}")
print("  " + "-" * 65)

distances = [0.06, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25]
for dist in distances:
    wins = stops = losses = 0
    pnl_total = 0.0
    for t in trades_base:
        sl  = round(t['ep'] - dist, 4)
        res = simulate_trade(t['snaps'], t['idx'], t['ep'], t['el_side'], sl)
        pnl_total += res['pnl']
        if res['outcome'] in ('WIN', 'PP'):
            wins += 1
        elif res['outcome'] == 'STOP':
            stops += 1
        else:
            losses += 1
    wr  = wins / n_total
    avg = pnl_total / n_total
    print(f"  {dist:>8.2f}  {n_total:>7}  {wins:>7}  {stops:>6}  {losses:>6}  "
          f"{wr:>6.1%}  {pnl_total:>+9.2f}  {avg:>+7.3f}")

# ── C) Stop dinâmico só para entradas abaixo de 0.82, fixo acima ─────────────
print(f"\nC) HIBRIDO: stop dinamico para ep < 0.82 / stop 0.65 para ep >= 0.82\n")
print(f"  {'Dist':>8}  {'Trades':>7}  {'WIN+PP':>7}  {'STOP':>6}  {'LOSS':>6}  {'WR':>6}  {'PnL':>9}  {'avg':>7}")
print("  " + "-" * 65)

for dist in [0.08, 0.10, 0.12, 0.15, 0.18]:
    wins = stops = losses = 0
    pnl_total = 0.0
    for t in trades_base:
        if t['ep'] >= 0.82:
            sl = 0.65
        else:
            sl = round(t['ep'] - dist, 4)
        res = simulate_trade(t['snaps'], t['idx'], t['ep'], t['el_side'], sl)
        pnl_total += res['pnl']
        if res['outcome'] in ('WIN', 'PP'):
            wins += 1
        elif res['outcome'] == 'STOP':
            stops += 1
        else:
            losses += 1
    wr  = wins / n_total
    avg = pnl_total / n_total
    print(f"  {dist:>8.2f}  {n_total:>7}  {wins:>7}  {stops:>6}  {losses:>6}  "
          f"{wr:>6.1%}  {pnl_total:>+9.2f}  {avg:>+7.3f}")

# ── D) Tabela por faixa de EP — stop dinâmico 0.12 ───────────────────────────
print(f"\nD) DETALHE POR FAIXA de EP — stop dinamico entry-0.12\n")
print(f"  {'Faixa EP':>14}  {'n':>4}  {'WIN+PP':>7}  {'STOP':>6}  {'LOSS':>6}  {'WR':>6}  {'PnL':>9}")
print("  " + "-" * 58)
for lo, hi in [(0.60,0.75),(0.75,0.80),(0.80,0.82),(0.82,0.86),(0.86,0.92),(0.92,1.01)]:
    sub = [t for t in trades_base if lo <= t['ep'] < hi]
    if not sub:
        continue
    wins = stops = losses = 0
    pnl_total = 0.0
    for t in sub:
        sl  = round(t['ep'] - 0.12, 4)
        res = simulate_trade(t['snaps'], t['idx'], t['ep'], t['el_side'], sl)
        pnl_total += res['pnl']
        if res['outcome'] in ('WIN', 'PP'):
            wins += 1
        elif res['outcome'] == 'STOP':
            stops += 1
        else:
            losses += 1
    wr = wins / len(sub) if sub else 0
    nota = ' <- atual' if lo == 0.82 else ''
    print(f"  [{lo:.2f} – {hi:.2f})  {len(sub):>4}  {wins:>7}  {stops:>6}  {losses:>6}  "
          f"{wr:>6.1%}  {pnl_total:>+9.2f}{nota}")

# Referência estratégia atual (só trades existentes, entry 0.82-0.86)
print(f"\n  Referencia: estrategia atual (18 trades ep 0.82-0.86, stop 0.65)")
existing = [t for t in trades_base if not t['is_new']]
pnl_atual = 0.0
wins_atual = 0
for t in existing:
    res = simulate_trade(t['snaps'], t['idx'], t['real_ep'] or t['ep'], t['el_side'], 0.65)
    pnl_atual += res['pnl']
    if res['outcome'] in ('WIN', 'PP'):
        wins_atual += 1
print(f"  {len(existing)} trades  {wins_atual} WIN+PP  WR={wins_atual/len(existing):.1%}  PnL={pnl_atual:>+.2f}")
