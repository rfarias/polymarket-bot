"""
_sim_real_runner.py
Simula a lógica de saída do live_early_entry_real_v1.py (shadow mode)
contra os logs de paper do final de semana.

Diferenças principais entre paper e runner real:
  1. Hedge: paper executa hedge quando el_bid < 0.50.
     Runner real: priority 3 (stop) captura el_bid < 0.65 ANTES da priority 4 (hedge),
     tornando o hedge praticamente código morto (só ativa se el_bid == 0 exatamente).
  2. PP GTC: shadow mode fecha na mesma poll (sem diferença prática).
  3. Entry fill: shadow assume fill imediato (igual ao paper).
  4. Hedge dinâmico: quando el_bid == 0, compara hedge_pnl vs stop_pnl antes de decidir.
"""
import json
from pathlib import Path
from collections import Counter, defaultdict

QTY          = 6.0
EE_STOP      = 0.65   # EE_STOP_LEVEL
EE_PP_BID    = 0.88   # EE_PROFIT_PROTECT_BID
EE_PP_SECS   = 70     # EE_PROFIT_PROTECT_SECS
EE_HEDGE_THR = 0.50   # EE_HEDGE_THR
EE_WIN_SECS  = 35     # secs <= 35 -> resolução final

sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))


def el_b(snap, side):  return snap['up_bid']   if side == 'UP' else snap['down_bid']
def opp_b(snap, side): return snap['down_bid'] if side == 'UP' else snap['up_bid']


def real_runner_exit(snaps_chrono, ep, side, qty):
    """
    Reproduz a lógica de saída do runner real (linhas 799-860 do source).
    snaps_chrono: snaps em ordem cronológica (secs decrescente, entry_secs -> 0).

    Retorna: (outcome, exit_price, pnl, exit_secs)

    Prioridade de saída (identica ao runner real):
      P1: secs <= 35 -> WIN ou REVERSAL
      P2: 36 <= secs <= 70 e el >= 0.88 -> PROFIT_PROTECT
      P3: 0 < el < 0.65 -> STOP_LOSS  ← captura antes do hedge!
      P4: el == 0 e opp > 0 -> hedge dinâmico (hedge_pnl vs stop_pnl)
    """
    for s in snaps_chrono:
        secs = s['secs']
        el   = el_b(s, side)
        opp  = opp_b(s, side)

        # P1 — resolução final
        if secs <= EE_WIN_SECS:
            if el >= 0.85:
                return ('WIN', 1.0, round((1.0 - ep) * qty, 2), secs)
            elif opp >= 0.85:
                exit_p = max(el, 0.001)
                return ('REVERSAL', exit_p, round((exit_p - ep) * qty, 2), secs)

        # P2 — profit protect
        elif EE_WIN_SECS < secs <= EE_PP_SECS and el >= EE_PP_BID:
            return ('PROFIT_PROTECT', el, round((el - ep) * qty, 2), secs)

        # P3 — stop loss (captura el < 0.65, incluindo el < 0.50)
        elif 0 < el < EE_STOP:
            return ('STOP_LOSS', el, round((el - ep) * qty, 2), secs)

        # P4 — hedge dinâmico: só alcançável quando el_bid == 0 exatamente
        # (el < 0.50 AND el > 0 já foi capturado pela P3 acima)
        elif el == 0 and opp > 0:
            stop_pnl  = (0.0 - ep) * qty
            hedge_pnl = (1.0 - ep - opp) * qty
            if hedge_pnl > stop_pnl:
                return ('WIN_HEDGE', opp, round(hedge_pnl, 2), secs)
            else:
                return ('STOP_LOSS', 0.0, round(stop_pnl, 2), secs)

    return ('MISSED', 0.0, 0.0, 0)


# ── Carregar logs e simular ────────────────────────────────────────────────────
records = []

for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]

    slug_snaps    = defaultdict(list)
    paper_entries = {}
    paper_closed  = {}

    for r in rows:
        t = r.get('type')
        if t == 'snapshot':
            slug  = r.get('current_slug', '')
            secs  = r.get('current_secs')
            exec_ = r.get('current_exec', {})
            if slug and secs is not None:
                slug_snaps[slug].append({
                    'secs':     secs,
                    'up_bid':   exec_.get('up_bid', 0),
                    'down_bid': exec_.get('down_bid', 0),
                })
        elif t == 'ee_paper_entry':
            paper_entries[r.get('slug', '')] = r
        elif t == 'ee_paper_closed':
            paper_closed[r.get('slug', '')] = r

    for slug, entry in paper_entries.items():
        closed = paper_closed.get(slug)
        if not closed:
            continue
        ee = closed.get('ee', {})
        paper_outcome = ee.get('outcome', '?')
        if paper_outcome in ('aberta', 'MISSED'):
            continue

        paper_pnl  = ee.get('pnl', 0)
        ep         = entry.get('ep', 0)
        side       = entry.get('side', '')
        entry_secs = entry.get('secs', 180)
        el_vel     = entry.get('el', {}).get('el_vel', 0)

        if not ep or not side:
            continue

        # Snaps do hold: entry_secs -> 0, ordem cronológica (secs decrescente)
        hold = sorted(
            [s for s in slug_snaps.get(slug, []) if s['secs'] <= entry_secs],
            key=lambda s: s['secs'],
            reverse=True,
        )

        real_outcome, real_exit_p, real_pnl, real_secs = real_runner_exit(
            hold, ep, side, QTY
        )

        records.append({
            'slug':           slug[-6:],
            'ep':             ep,
            'side':           side,
            'entry_secs':     entry_secs,
            'el_vel':         el_vel,
            'paper_outcome':  paper_outcome,
            'paper_pnl':      paper_pnl,
            'real_outcome':   real_outcome,
            'real_pnl':       real_pnl,
            'real_exit_p':    real_exit_p,
            'real_exit_secs': real_secs,
            'changed':        paper_outcome != real_outcome,
            'pnl_delta':      round(real_pnl - paper_pnl, 2),
        })

# ── Relatório ─────────────────────────────────────────────────────────────────
n           = len(records)
paper_total = round(sum(r['paper_pnl'] for r in records), 2)
real_total  = round(sum(r['real_pnl']  for r in records), 2)
delta_total = round(real_total - paper_total, 2)
changed     = [r for r in records if r['changed']]

p_wins = sum(1 for r in records if r['paper_pnl'] > 0)
r_wins = sum(1 for r in records if r['real_pnl']  > 0)

print(f"\n{'='*70}")
print(f"  SIMULAÇÃO RUNNER REAL vs PAPER — {n} trades")
print(f"{'='*70}")
print(f"  PnL paper  : {paper_total:>+.2f}  WR={p_wins}/{n}={p_wins/n:.1%}  avg={paper_total/n:>+.3f}")
print(f"  PnL real   : {real_total:>+.2f}  WR={r_wins}/{n}={r_wins/n:.1%}  avg={real_total/n:>+.3f}")
print(f"  Delta      : {delta_total:>+.2f}")
print(f"  Unchanged  : {n - len(changed)}/{n}   Changed: {len(changed)}/{n}")

# Distribuição de outcomes
p_cnt = Counter(r['paper_outcome'] for r in records)
r_cnt = Counter(r['real_outcome']  for r in records)
all_outs = sorted(set(list(p_cnt) + list(r_cnt)))

print(f"\n  {'Outcome':>20}  {'Paper':>6}  {'Real':>6}  {'dif':>4}")
print("  " + "-"*42)
for out in all_outs:
    diff = r_cnt.get(out, 0) - p_cnt.get(out, 0)
    sign = f'{diff:+d}' if diff else '  0'
    print(f"  {out:>20}  {p_cnt.get(out, 0):>6}  {r_cnt.get(out, 0):>6}  {sign:>4}")

# Detalhe das trades alteradas
if changed:
    print(f"\n  TRADES ALTERADAS ({len(changed)}):")
    print(f"  {'Slug':>6}  {'EP':>5}  {'secs':>4}  {'vel':>5}  "
          f"{'Paper outcome':>16}  {'Real outcome':>16}  "
          f"{'Pnl P':>7}  {'PnL R':>7}  {'dif':>7}")
    print("  " + "-"*82)
    for r in sorted(changed, key=lambda x: x['paper_outcome']):
        print(f"  {r['slug']:>6}  {r['ep']:.3f}  {r['entry_secs']:>4}  {r['el_vel']:>5.3f}  "
              f"{r['paper_outcome']:>16}  {r['real_outcome']:>16}  "
              f"{r['paper_pnl']:>+7.2f}  {r['real_pnl']:>+7.2f}  {r['pnl_delta']:>+7.2f}")

# WIN_HEDGE convertidos em STOP_LOSS (impacto do bug de prioridade)
hedge_conv = [r for r in changed if r['paper_outcome'] == 'WIN_HEDGE']
if hedge_conv:
    hc_pp = sum(r['paper_pnl'] for r in hedge_conv)
    hc_rp = sum(r['real_pnl']  for r in hedge_conv)
    print(f"\n  WIN_HEDGE -> STOP_LOSS (runner real):  {len(hedge_conv)} trades")
    print(f"    Paper: {hc_pp:>+.2f}  Real: {hc_rp:>+.2f}  delta: {hc_rp-hc_pp:>+.2f}")
    print(f"    Motivo: priority 3 (stop el<0.65) captura antes da priority 4 (hedge el<0.50)")
    print(f"    Fix sugerido: mover verificação de hedge ANTES do stop no runner real")
    for r in hedge_conv:
        print(f"      {r['slug']}  ep={r['ep']:.3f}  el_vel={r['el_vel']:.3f}  "
              f"exit_p={r['real_exit_p']:.3f}@secs={r['real_exit_secs']}  "
              f"paper={r['paper_pnl']:>+.2f}  real={r['real_pnl']:>+.2f}")

# Verificar se há outros tipos de mudança
other_changed = [r for r in changed if r['paper_outcome'] != 'WIN_HEDGE']
if other_changed:
    print(f"\n  Outras mudanças ({len(other_changed)}):")
    for r in other_changed:
        print(f"    {r['slug']}  {r['paper_outcome']} -> {r['real_outcome']}  "
              f"delta={r['pnl_delta']:>+.2f}")

# STOP_LOSS: distribuição de gaps (exit real vs 0.65 esperado)
real_stops = [r for r in records if r['real_outcome'] == 'STOP_LOSS']
if real_stops:
    gaps = [round(0.65 - r['real_exit_p'], 3) for r in real_stops if r['real_exit_p'] > 0]
    paper_stops = [r for r in records if r['paper_outcome'] == 'STOP_LOSS']
    paper_gaps  = [round(0.65 - (r['ep'] + r['paper_pnl']/QTY), 3) for r in paper_stops]
    print(f"\n  STOP_LOSS comparison:")
    if paper_gaps:
        print(f"    Paper stops: {len(paper_stops)}  avg_gap={sum(paper_gaps)/len(paper_gaps):.3f}")
    else:
        print(f"    Paper stops: {len(paper_stops)}  (sem gaps)")
    if gaps:
        print(f"    Real  stops: {len(real_stops)}   avg_gap={sum(gaps)/len(gaps):.3f}")
    else:
        print(f"    Real  stops: {len(real_stops)}   (sem gaps com exit_p>0)")

print(f"\n{'='*70}")
print(f"  RESUMO EXECUTIVO")
print(f"{'='*70}")
print(f"  122 trades simulados: paper={paper_total:>+.2f}  runner_real={real_total:>+.2f}")
if delta_total < 0:
    print(f"  Runner real seria {abs(delta_total):.2f} PIOR que o paper ({len(changed)} trades diferentes)")
elif delta_total > 0:
    print(f"  Runner real seria {delta_total:.2f} MELHOR que o paper ({len(changed)} trades diferentes)")
else:
    print(f"  Runner real teria resultado IDÊNTICO ao paper")

if hedge_conv:
    print(f"\n  BUG IDENTIFICADO: priority 4 (hedge) nunca ativa no runner real.")
    print(f"  Qualquer el_bid entre 0 e 0.65 aciona a priority 3 (stop) primeiro.")
    print(f"  Impacto: {len(hedge_conv)} WIN_HEDGE viram STOP_LOSS = delta {hc_rp-hc_pp:>+.2f}")
    print(f"\n  FIX: mover o bloco elif hedge (P4) para ANTES do elif stop (P3)")
    print(f"       em live_early_entry_real_v1.py linhas ~830-860")
