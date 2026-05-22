"""
Simula entrar no primeiro snap com signal_ok=True, qualquer que seja o bid EL.
Compara com a estratégia atual (entrada apenas em 0.82-0.86).

Dados: snapshots dos logs ee_paper (runner v1/v2).
Outcome: determinado pelos closed events (quando existem) ou pela trajetória
         do bid no final do slug (el_bid>=0.99 = WIN, opp_bid>=0.99 = REVERSAL).
"""
from __future__ import annotations
import json
from pathlib import Path
from collections import defaultdict

QTY          = 6.0
STOP_LEVEL   = 0.65   # stop conforme runner v2
PP_BID       = 0.88   # profit protect conforme runner v2
PP_SECS      = 70

sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

# ── Coleta de dados brutos por slug ─────────────────────────────────────────
slug_snaps:   dict[str, list[dict]] = defaultdict(list)
slug_closed:  dict[str, dict]       = {}
slug_entry:   dict[str, dict]       = {}   # entradas reais (runner)

for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
    for r in rows:
        t = r.get('type')
        slug = r.get('slug') or r.get('current_slug') or ''
        if not slug:
            continue

        if t == 'snapshot':
            el   = r.get('early_leader') or {}
            exec_ = r.get('current_exec') or {}
            slug_snaps[slug].append({
                'secs':     r.get('current_secs'),
                'up_bid':   exec_.get('up_bid', 0),
                'dn_bid':   exec_.get('down_bid', 0),
                'el_side':  el.get('early_side'),
                'signal_ok': el.get('signal_ok', False),
                'el_vel':   el.get('el_vel', 0),
                'ee_state': (r.get('ee_position') or {}).get('state', 'none'),
            })
        elif t == 'ee_paper_entry':
            slug_entry[slug] = r
        elif t == 'ee_paper_closed':
            ee = r.get('ee') or {}
            if ee.get('outcome') and ee['outcome'] != 'MISSED':
                slug_closed[slug] = ee


def _el_bid(snap: dict) -> float:
    side = snap.get('el_side')
    if side == 'UP':   return snap.get('up_bid', 0)
    if side == 'DOWN': return snap.get('dn_bid', 0)
    return 0.0

def _opp_bid(snap: dict) -> float:
    side = snap.get('el_side')
    if side == 'UP':   return snap.get('dn_bid', 0)
    if side == 'DOWN': return snap.get('up_bid', 0)
    return 0.0


def _determine_outcome_from_snaps(snaps: list[dict], entry_idx: int) -> tuple[str, float]:
    """Determina outcome e exit_price da trajetória após a entrada."""
    hold = snaps[entry_idx:]
    side = hold[0]['el_side'] if hold else None

    # aplica lógica stop + PP + resolução
    for s in hold:
        secs = s.get('secs') or 999
        el   = _el_bid(s)
        opp  = _opp_bid(s)

        if secs <= 35:
            if el >= 0.85:
                return 'WIN', 1.0
            elif opp >= 0.85:
                return 'REVERSAL', 0.0

        if 36 <= secs <= PP_SECS and el >= PP_BID:
            return 'PROFIT_PROTECT', el

        if el < STOP_LEVEL and el > 0:
            return 'STOP_LOSS', el

        if el < 0.50 and opp > 0:
            return 'WIN_HEDGE', opp   # simplificado — pega opp no momento do hedge

    return 'UNKNOWN', 0.0


# ── Simulação: 1ª entrada com signal_ok ─────────────────────────────────────
sim_trades    = []   # novos trades (signal_ok em qualquer bid)
missed_signal = []   # slugs com signal que nunca tiveram entry real

for slug, snaps in slug_snaps.items():
    # ordena por secs decrescente (mais cedo primeiro)
    snaps_sorted = sorted(snaps, key=lambda s: s.get('secs') or 0, reverse=True)

    # 1º snap com signal_ok e bid EL disponível
    first_signal_idx = None
    for i, s in enumerate(snaps_sorted):
        if s.get('signal_ok') and _el_bid(s) > 0:
            first_signal_idx = i
            break

    if first_signal_idx is None:
        continue

    entry_snap = snaps_sorted[first_signal_idx]
    ep         = round(_el_bid(entry_snap), 4)
    el_side    = entry_snap.get('el_side')
    entry_secs = entry_snap.get('secs')
    el_vel     = entry_snap.get('el_vel', 0)

    # outcome real (runner) se existir
    real_closed  = slug_closed.get(slug)
    real_ep      = slug_entry.get(slug, {}).get('ep')
    real_outcome = real_closed['outcome'] if real_closed else None
    real_pnl     = real_closed['pnl']    if real_closed else None

    # outcome simulado a partir da trajetória
    sim_outcome, exit_ep = _determine_outcome_from_snaps(snaps_sorted, first_signal_idx)

    # calcula PnL simulado
    if sim_outcome == 'WIN':
        pnl_sim = round((1.0 - ep) * QTY, 4)
    elif sim_outcome == 'REVERSAL':
        pnl_sim = round((0.0 - ep) * QTY, 4)
    elif sim_outcome == 'STOP_LOSS':
        pnl_sim = round((exit_ep - ep) * QTY, 4)
    elif sim_outcome == 'PROFIT_PROTECT':
        pnl_sim = round((exit_ep - ep) * QTY, 4)
    elif sim_outcome == 'WIN_HEDGE':
        # hedge: entry perde (0-ep)*qty + hedge ganha (1-exit_ep)*qty aprox
        pnl_sim = round((0.0 - ep) * QTY + (1.0 - exit_ep) * QTY, 4)
    else:
        pnl_sim = 0.0

    # PnL real recalculado com ep simulado (para trades que tinham entry real)
    pnl_real_recosted = None
    if real_closed and real_ep:
        delta_ep = ep - real_ep   # negativo se entramos mais barato
        if real_outcome == 'WIN':
            pnl_real_recosted = round((1.0 - ep) * QTY, 4)
        elif real_outcome in ('WIN_HEDGE',):
            pnl_real_recosted = real_pnl + (-delta_ep * QTY)  # entry mais barata muda o pnl
            pnl_real_recosted = round(pnl_real_recosted, 4)
        elif real_outcome == 'STOP_LOSS':
            pnl_real_recosted = round((STOP_LEVEL - ep) * QTY, 4)
        elif real_outcome == 'PROFIT_PROTECT':
            pnl_real_recosted = real_pnl + (-delta_ep * QTY)
            pnl_real_recosted = round(pnl_real_recosted, 4)
        else:
            pnl_real_recosted = real_pnl

    # min_bid durante o hold (para validar stop)
    hold_snaps = snaps_sorted[first_signal_idx:]
    el_bids    = [_el_bid(s) for s in hold_snaps if _el_bid(s) > 0]
    min_bid    = round(min(el_bids), 3) if el_bids else None

    sim_trades.append({
        'slug':            slug[-6:],
        'el_side':         el_side,
        'ep_sim':          ep,
        'ep_real':         real_ep,
        'entry_secs':      entry_secs,
        'el_vel':          el_vel,
        'sim_outcome':     sim_outcome,
        'pnl_sim':         pnl_sim,
        'real_outcome':    real_outcome,
        'real_pnl':        real_pnl,
        'pnl_recosted':    pnl_real_recosted,
        'min_bid':         min_bid,
        'is_new':          real_ep is None,   # trade que não existia antes
    })

sim_trades.sort(key=lambda t: t['slug'])

# ── Saída ────────────────────────────────────────────────────────────────────
existing = [t for t in sim_trades if not t['is_new']]
new_only  = [t for t in sim_trades if t['is_new']]

print(f"\n{'='*80}")
print(f"  SIMULAÇÃO: entrada no 1º signal_ok — qualquer bid")
print(f"{'='*80}\n")

# Tabela trades existentes (entry real recalculada)
print(f"── Trades com entry real (ep recalculado para 1º signal_ok) ──\n")
print(f"{'Slug':>8}  {'EP_real':>8}  {'EP_sim':>7}  {'Δep':>6}  "
      f"{'secs':>5}  {'el_vel':>7}  {'Outcome':>14}  {'PnL real':>9}  {'PnL sim':>8}  {'min_bid':>8}  {'<stop':>6}")
print('-' * 100)
for t in existing:
    dep  = round((t['ep_sim'] or 0) - (t['ep_real'] or 0), 3)
    mbs  = t['min_bid']
    stop_hit = 'SIM' if mbs is not None and mbs < STOP_LEVEL else '-'
    print(f"{t['slug']:>8}  {str(t['ep_real']):>8}  {t['ep_sim']:>7.3f}  {dep:>+6.3f}  "
          f"{str(t['entry_secs']):>5}  {t['el_vel']:>7.3f}  {t['real_outcome']:>14}  "
          f"{str(t['real_pnl']):>9}  {str(t['pnl_recosted']):>8}  {str(mbs):>8}  {stop_hit:>6}")
print('-' * 100)

pnl_real_total = round(sum(t['real_pnl'] for t in existing), 2)
pnl_sim_total  = round(sum(t['pnl_recosted'] for t in existing if t['pnl_recosted'] is not None), 2)
print(f"\n  PnL base (entradas reais)  : {pnl_real_total:>+8.2f}")
print(f"  PnL simulado (ep mais cedo): {pnl_sim_total:>+8.2f}  (delta: {pnl_sim_total-pnl_real_total:>+.2f})\n")

# Tabela trades novos (nunca capturados antes)
if new_only:
    print(f"\n── Trades novos (signal_ok mas bid nunca chegou a 0.82–0.86) ──\n")
    print(f"{'Slug':>8}  {'EP_sim':>7}  {'secs':>5}  {'el_vel':>7}  "
          f"{'Sim_out':>14}  {'PnL sim':>8}  {'min_bid':>8}  {'<stop':>6}")
    print('-' * 72)
    for t in new_only:
        mbs  = t['min_bid']
        stop_hit = 'SIM' if mbs is not None and mbs < STOP_LEVEL else '-'
        print(f"{t['slug']:>8}  {t['ep_sim']:>7.3f}  {str(t['entry_secs']):>5}  {t['el_vel']:>7.3f}  "
              f"{t['sim_outcome']:>14}  {t['pnl_sim']:>+8.2f}  {str(mbs):>8}  {stop_hit:>6}")
    print('-' * 72)
    pnl_new = round(sum(t['pnl_sim'] for t in new_only), 2)
    wr_new  = sum(1 for t in new_only if t['pnl_sim'] > 0)
    print(f"\n  Trades novos: {len(new_only)}  |  WR: {wr_new}/{len(new_only)}  |  PnL: {pnl_new:>+.2f}\n")

# Resumo combinado
print(f"\n{'='*80}")
print(f"  RESUMO COMPARATIVO")
print(f"{'='*80}\n")

all_sim_pnl  = round(sum(
    t['pnl_recosted'] if not t['is_new'] and t['pnl_recosted'] is not None else t['pnl_sim']
    for t in sim_trades
), 2)
all_sim_wins = sum(1 for t in sim_trades if (
    t['pnl_recosted'] if not t['is_new'] and t['pnl_recosted'] is not None else t['pnl_sim']
) > 0)
n_sim = len(sim_trades)

print(f"  Estratégia ATUAL (0.82-0.86):")
print(f"    Trades: {len(existing)}  |  PnL: {pnl_real_total:>+.2f}")
print(f"\n  Estratégia SIMULADA (qualquer bid com signal_ok):")
print(f"    Trades existentes recalculados: {len(existing)}  |  PnL: {pnl_sim_total:>+.2f}")
if new_only:
    print(f"    Trades novos adicionados: {len(new_only)}")
print(f"    Total simulado: {n_sim} trades  |  {all_sim_wins} lucrativos  |  PnL: {all_sim_pnl:>+.2f}")
print(f"\n  Delta vs atual: {all_sim_pnl - pnl_real_total:>+.2f}\n")

# Distribuição de ep_sim
print(f"  Distribuição dos EPs simulados (1º signal_ok):")
ep_buckets = [(0.60,0.70),(0.70,0.75),(0.75,0.80),(0.80,0.82),(0.82,0.86),(0.86,0.92),(0.92,1.01)]
for lo, hi in ep_buckets:
    n = sum(1 for t in sim_trades if lo <= t['ep_sim'] < hi)
    wins_b = sum(1 for t in sim_trades if lo <= t['ep_sim'] < hi and (
        (t['pnl_recosted'] if t['pnl_recosted'] is not None else t['pnl_sim'])) > 0)
    bar = '█' * n
    nota = ' <-- faixa atual' if lo == 0.82 else ''
    print(f"    [{lo:.2f}–{hi:.2f})  {n:>3} trades  {wins_b}W  {bar}{nota}")
