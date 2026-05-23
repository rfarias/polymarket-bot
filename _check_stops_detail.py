"""
Detalha os 3 STOP_LOSS:
1. Busca o evento ee_paper_stop_loss (tem el_bid e secs no momento exato)
2. Busca o snapshot mais próximo para pegar opp_bid → viabilidade do hedge
3. Analisa se PP com janela mais larga (ex: secs<=120) teria protegido
"""
import json, time
from pathlib import Path
from collections import defaultdict

QTY      = 6.0
PP_BID   = 0.88

sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))

stop_events: list[dict] = []
slug_snaps: dict[str, list] = defaultdict(list)
slug_entry: dict[str, dict] = {}

for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
    for r in rows:
        t    = r.get('type')
        slug = r.get('slug') or r.get('current_slug') or ''
        if t == 'ee_paper_stop_loss':
            stop_events.append(r)
        elif t == 'snapshot':
            exec_ = r.get('current_exec') or {}
            el    = r.get('early_leader') or {}
            if slug:
                slug_snaps[slug].append({
                    'ts':      r.get('ts', 0),
                    'secs':    r.get('current_secs'),
                    'up_bid':  exec_.get('up_bid', 0),
                    'dn_bid':  exec_.get('down_bid', 0),
                    'el_side': el.get('early_side'),
                    'ee_state': (r.get('ee_position') or {}).get('state', 'none'),
                })
        elif t == 'ee_paper_entry' and slug:
            slug_entry[slug] = r

print(f"Stop events encontrados: {len(stop_events)}\n")

for se in stop_events:
    slug     = se.get('slug', '')
    el_bid_s = se.get('el_bid', 0)
    secs_s   = se.get('secs', 0)
    ts_s     = se.get('ts', 0)
    entry    = slug_entry.get(slug, {})
    ep       = entry.get('ep', 0)
    side     = entry.get('side', '')

    def el(s): return s.get('up_bid',0) if side=='UP' else s.get('dn_bid',0)
    def opp(s): return s.get('dn_bid',0) if side=='UP' else s.get('up_bid',0)

    # Snap mais próximo ao momento do stop (por ts)
    snaps = slug_snaps.get(slug, [])
    nearest = min(snaps, key=lambda s: abs(s.get('ts',0) - ts_s)) if snaps else None
    opp_at_stop = opp(nearest) if nearest else None
    el_at_stop  = el(nearest)  if nearest else el_bid_s

    # PnL alternativas
    pnl_stop  = round((el_bid_s - ep) * QTY, 2)
    pnl_hedge = round((0.0 - ep)*QTY + (1.0 - (opp_at_stop or 0))*QTY, 2) if opp_at_stop else None

    # PP: checar peak bid em cada janela de secs
    entry_snaps = sorted([s for s in snaps if s.get('ee_state') == 'entry'],
                         key=lambda s: s.get('secs') or 0, reverse=True)
    pp_windows = [
        ('atual (36-70)',   36,  70),
        ('ampliado (36-110)', 36, 110),
        ('ampliado (36-150)', 36, 150),
        ('ampliado (36-180)', 36, 180),
    ]

    print(f"{'='*68}")
    print(f"  {slug[-6:]}  side={side}  EP={ep:.3f}  "
          f"el_bid no stop={el_bid_s:.3f}  secs={secs_s}")
    print(f"{'='*68}")
    print(f"  PnL stop real       : {pnl_stop:>+.2f}  (saiu em {el_bid_s:.3f})")
    if opp_at_stop is not None:
        liq = 'OK' if opp_at_stop >= 0.25 else 'BAIXA'
        print(f"  opp_bid no crash    : {opp_at_stop:.3f}  [{liq}]")
        print(f"  PnL se hedge        : {pnl_hedge:>+.2f}  "
              f"(EL perde tudo, opp ganha 1.0-{opp_at_stop:.3f})")
        print(f"  vs stop real        : delta {pnl_hedge - pnl_stop:>+.2f}")

    print(f"\n  Análise PP por janela (bid pico >= {PP_BID}):")
    for label, lo, hi in pp_windows:
        candidates = [s for s in entry_snaps if lo <= (s.get('secs') or 0) <= hi and el(s) >= PP_BID]
        if candidates:
            best = max(candidates, key=lambda s: el(s))
            pnl_pp = round((el(best) - ep) * QTY, 2)
            print(f"    {label:25s}: SIM  bid={el(best):.3f} secs={best['secs']}  "
                  f"PnL={pnl_pp:>+.2f}")
        else:
            peak = max((el(s) for s in entry_snaps if lo <= (s.get('secs') or 0) <= hi), default=0)
            print(f"    {label:25s}: NAO  (pico={peak:.3f} < {PP_BID})")
    print()

# ── Impacto de ampliar a janela PP nos 19 trades actuais ──────────────────────
print(f"\n{'='*68}")
print(f"  IMPACTO de ampliar PP_SECS nos trades existentes")
print(f"  (quantos WIN teriam virado PP antecipado, perdendo ticks?)")
print(f"{'='*68}\n")

pp_events: list[dict] = []
win_events: list[dict] = []
closed_events: dict[str, dict] = {}

for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
    for r in rows:
        t = r.get('type')
        if t == 'ee_paper_closed':
            ee = r.get('ee') or {}
            closed_events[r.get('slug','')] = ee

for secs_limit in [70, 90, 110, 130, 150]:
    fired_pp = 0
    pnl_total = 0.0
    for slug, ee in closed_events.items():
        if ee.get('outcome') not in ('WIN', 'PROFIT_PROTECT'):
            continue
        ep   = ee.get('entry_ep', 0)
        side_e = ee.get('entry_side', '')
        if not ep or not side_e:
            continue
        def el2(s): return s.get('up_bid',0) if side_e=='UP' else s.get('dn_bid',0)

        snaps = sorted(slug_snaps.get(slug, []), key=lambda s: s.get('secs') or 0, reverse=True)
        entry_snaps_w = [s for s in snaps if s.get('ee_state') == 'entry']

        # Teria PP ativado nesta janela?
        pp_cand = next((s for s in entry_snaps_w
                        if 36 <= (s.get('secs') or 0) <= secs_limit and el2(s) >= PP_BID), None)
        if pp_cand:
            fired_pp += 1
            pnl_total += round((el2(pp_cand) - ep) * QTY, 2)
        else:
            pnl_total += ee.get('pnl', 0)

    n_eligible = sum(1 for ee in closed_events.values()
                     if ee.get('outcome') in ('WIN', 'PROFIT_PROTECT'))
    print(f"  PP_SECS <= {secs_limit:3d}: "
          f"{fired_pp:2d}/{n_eligible} dispararam PP  PnL WIN+PP = {pnl_total:>+.2f}")
