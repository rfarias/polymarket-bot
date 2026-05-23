"""
Para os 3 trades STOP_LOSS, analisa:
1. O bid chegou a >= 0.88 antes de cair? (PP teria protegido?)
2. Qual era o opp_bid no momento do stop? (hedge seria viável?)
3. Compara PnL: stop real vs hedge vs PP (se disponível)
"""
import json
from pathlib import Path
from collections import defaultdict

QTY      = 6.0
PP_BID   = 0.88
PP_SECS  = 70
STOP_LVL = 0.65

sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

slug_snaps:  dict[str, list] = defaultdict(list)
slug_entry:  dict[str, dict] = {}
slug_closed: dict[str, dict] = {}

for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
    for r in rows:
        t    = r.get('type')
        slug = r.get('slug') or r.get('current_slug') or ''
        if not slug:
            continue
        if t == 'snapshot':
            exec_ = r.get('current_exec') or {}
            el    = r.get('early_leader') or {}
            slug_snaps[slug].append({
                'secs':    r.get('current_secs'),
                'up_bid':  exec_.get('up_bid', 0),
                'dn_bid':  exec_.get('down_bid', 0),
                'el_side': el.get('early_side'),
                'ee_state': (r.get('ee_position') or {}).get('state', 'none'),
            })
        elif t == 'ee_paper_entry':
            slug_entry[slug] = r
        elif t == 'ee_paper_closed':
            ee = r.get('ee') or {}
            slug_closed[slug] = ee

# Filtra só os STOP_LOSS
stop_slugs = {slug for slug, ee in slug_closed.items() if ee.get('outcome') == 'STOP_LOSS'}

for slug in sorted(stop_slugs):
    entry = slug_entry.get(slug, {})
    ee    = slug_closed[slug]
    ep    = entry.get('ep', 0)
    side  = entry.get('side') or entry.get('el_side') or ee.get('entry_side', '')
    pnl_real = ee.get('pnl', 0)
    exit_real = round(ep + pnl_real / QTY, 3)

    snaps = sorted(slug_snaps.get(slug, []), key=lambda s: s.get('secs') or 0, reverse=True)

    def el_bid(s):
        return s.get('up_bid', 0) if side == 'UP' else s.get('dn_bid', 0)
    def opp_bid(s):
        return s.get('dn_bid', 0) if side == 'UP' else s.get('up_bid', 0)

    # Snaps durante estado "entry"
    entry_snaps = [s for s in snaps if s.get('ee_state') == 'entry']

    print(f"\n{'='*70}")
    print(f"  STOP_LOSS  slug={slug[-6:]}  side={side}  EP={ep:.3f}  "
          f"exit_real={exit_real:.3f}  PnL={pnl_real:>+.2f}")
    print(f"{'='*70}")

    # Tabela tick a tick durante o hold
    print(f"\n  Trajetória durante hold (estado entry):")
    print(f"  {'secs':>5}  {'el_bid':>7}  {'opp_bid':>8}  {'soma':>6}  nota")
    print("  " + "-" * 50)

    pp_snap     = None   # 1º snap onde PP poderia ter ativado
    stop_snap   = None   # 1º snap onde stop foi abaixo de 0.65
    peak_el     = 0.0
    peak_snap   = None

    for s in entry_snaps:
        secs = s.get('secs') or 0
        el   = el_bid(s)
        opp  = opp_bid(s)
        soma = round(el + opp, 3)

        nota = ''
        if el > peak_el:
            peak_el   = el
            peak_snap = s
        if 36 <= secs <= PP_SECS and el >= PP_BID and pp_snap is None:
            pp_snap = s
            nota = '<-- PP poderia ativar aqui'
        if el < STOP_LVL and stop_snap is None:
            stop_snap = s
            nota = '<-- STOP detectado aqui'
        if el < 0.10:
            nota = nota or '<-- crash'

        print(f"  {secs:>5}  {el:>7.3f}  {opp:>8.3f}  {soma:>6.3f}  {nota}")

    # Análise
    print(f"\n  Pico EL durante hold: {peak_el:.3f}  (secs={peak_snap.get('secs') if peak_snap else '?'})")

    # PP
    if pp_snap:
        pp_el   = el_bid(pp_snap)
        pnl_pp  = round((pp_el - ep) * QTY, 2)
        print(f"\n  [PP] Teria ativado em secs={pp_snap.get('secs')}  bid={pp_el:.3f}")
        print(f"       PnL com PP : {pnl_pp:>+.2f}  vs stop real {pnl_real:>+.2f}  "
              f"(delta {pnl_pp - pnl_real:>+.2f})")
    else:
        print(f"\n  [PP] NAO teria ativado (bid nunca chegou a {PP_BID} em secs 36-{PP_SECS})")
        print(f"       Pico maximo: {peak_el:.3f}")

    # Hedge no momento do stop
    if stop_snap:
        opp_at_stop = opp_bid(stop_snap)
        secs_stop   = stop_snap.get('secs', 0)
        # simula hedge: perde (0 - ep)*qty no EL + ganha (1 - opp_at_stop)*qty no opp
        pnl_hedge_entry = round((0.0 - ep) * QTY, 2)
        pnl_hedge_opp   = round((1.0 - opp_at_stop) * QTY, 2)
        pnl_hedge_total = round(pnl_hedge_entry + pnl_hedge_opp, 2)

        print(f"\n  [HEDGE] No momento do stop (secs={secs_stop}):")
        print(f"          opp_bid = {opp_at_stop:.3f}  (liquidez do lado oposto)")
        print(f"          EL perdeu (0.0 - {ep}) x6     = {pnl_hedge_entry:>+.2f}")
        print(f"          opp ganhou (1.0 - {opp_at_stop:.3f}) x6 = {pnl_hedge_opp:>+.2f}")
        print(f"          PnL hedge total               = {pnl_hedge_total:>+.2f}")
        print(f"          vs stop real                  = {pnl_real:>+.2f}  "
              f"(delta {pnl_hedge_total - pnl_real:>+.2f})")

        if opp_at_stop >= 0.30:
            print(f"          Liquidez OPP: {opp_at_stop:.3f} — razoavel para hedge")
        else:
            print(f"          Liquidez OPP: {opp_at_stop:.3f} — BAIXA, fill incerto")

# Resumo
print(f"\n{'='*70}")
print(f"  RESUMO — 3 STOP_LOSS")
print(f"{'='*70}")
print(f"  {'Slug':>8}  {'EP':>6}  {'Exit real':>10}  {'PnL stop':>9}  {'PnL PP':>8}  {'PnL hedge':>10}")
print("  " + "-" * 60)
for slug in sorted(stop_slugs):
    entry = slug_entry.get(slug, {})
    ee    = slug_closed[slug]
    ep    = entry.get('ep', 0)
    side  = entry.get('side') or ee.get('entry_side', '')
    pnl_real = ee.get('pnl', 0)
    exit_real = round(ep + pnl_real / QTY, 3)

    snaps = sorted(slug_snaps.get(slug, []), key=lambda s: s.get('secs') or 0, reverse=True)
    entry_snaps = [s for s in snaps if s.get('ee_state') == 'entry']

    def el_bid(s): return s.get('up_bid',0) if side=='UP' else s.get('dn_bid',0)
    def opp_bid(s): return s.get('dn_bid',0) if side=='UP' else s.get('up_bid',0)

    pp_snap   = next((s for s in entry_snaps
                      if 36 <= (s.get('secs') or 0) <= PP_SECS and el_bid(s) >= PP_BID), None)
    stop_snap = next((s for s in entry_snaps if el_bid(s) < STOP_LVL), None)

    pnl_pp  = round((el_bid(pp_snap) - ep) * QTY, 2) if pp_snap else None
    pnl_hg  = None
    if stop_snap:
        opp = opp_bid(stop_snap)
        pnl_hg = round((0.0 - ep)*QTY + (1.0 - opp)*QTY, 2)

    pp_str = f'{pnl_pp:>+.2f}' if pnl_pp is not None else '  n/a'
    hg_str = f'{pnl_hg:>+.2f}' if pnl_hg is not None else '  n/a'
    print(f"  {slug[-6:]:>8}  {ep:>6.3f}  {exit_real:>10.3f}  {pnl_real:>+9.2f}  "
          f"{pp_str:>8}  {hg_str:>10}")
