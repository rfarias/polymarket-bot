"""
_sim_btc_momentum.py
Analisa volatilidade e direcao do BTC nos 3 minutos ANTES de cada entrada EE.

Metricas calculadas via Binance 1m klines (3 candles antes da entrada):
  delta_1m_usd  -- variacao BTC no ultimo minuto antes da entrada
  delta_3m_usd  -- variacao BTC nos 3 minutos antes da entrada
  delta_1m_bps  -- idem em basis points
  delta_3m_bps
  range_3m_usd  -- amplitude high-low dos 3 minutos (volatilidade)
  range_3m_bps
  adverse_1m    -- True se BTC indo contra o lado EL no ultimo minuto
  adverse_3m    -- True se BTC indo contra o lado EL nos 3 minutos

Hipotese: adverse=True ou alta volatilidade -> maior risco de STOP_LOSS
"""
import json
import re
import time
import requests
import statistics
from pathlib import Path
from collections import defaultdict

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
TIMEOUT = 6
_cache: dict = {}


def _btc_pre_entry(ts_entry: float, side: str) -> dict:
    """Retorna metricas de direcao e volatilidade nos 3 candles 1m antes da entrada."""
    # 3 candles que terminam ANTES do minuto da entrada
    start_ms = (int(ts_entry // 60) - 3) * 60 * 1000
    key = f"mom_{start_ms}"
    if key in _cache:
        rows = _cache[key]
    else:
        try:
            r = requests.get(BINANCE_KLINES, params={
                "symbol": "BTCUSDT", "interval": "1m",
                "startTime": start_ms, "limit": 3,
            }, timeout=TIMEOUT)
            rows = r.json() or []
            _cache[key] = rows
            time.sleep(0.12)
        except Exception:
            _cache[key] = []
            return {}

    if len(rows) < 3:
        return {}

    # Cada row: [open_time, open, high, low, close, ...]
    closes  = [float(rows[i][4]) for i in range(3)]  # close M-3, M-2, M-1
    highs   = [float(rows[i][2]) for i in range(3)]
    lows    = [float(rows[i][3]) for i in range(3)]
    ref     = closes[-1]  # close do minuto imediatamente antes da entrada

    delta_1m_usd = round(closes[2] - closes[1], 2)     # M-1 vs M-2
    delta_3m_usd = round(closes[2] - closes[0], 2)     # M-1 vs M-3
    delta_1m_bps = round(delta_1m_usd / ref * 10000, 2) if ref else 0
    delta_3m_bps = round(delta_3m_usd / ref * 10000, 2) if ref else 0
    range_3m_usd = round(max(highs) - min(lows), 2)
    range_3m_bps = round(range_3m_usd / ref * 10000, 2) if ref else 0

    # Adverso: BTC vai na direcao OPOSTA ao lado EL
    if side == 'UP':
        adverse_1m = delta_1m_usd < 0
        adverse_3m = delta_3m_usd < 0
    else:
        adverse_1m = delta_1m_usd > 0
        adverse_3m = delta_3m_usd > 0

    return {
        'delta_1m_usd': delta_1m_usd,
        'delta_3m_usd': delta_3m_usd,
        'delta_1m_bps': delta_1m_bps,
        'delta_3m_bps': delta_3m_bps,
        'range_3m_usd': range_3m_usd,
        'range_3m_bps': range_3m_bps,
        'adverse_1m':   adverse_1m,
        'adverse_3m':   adverse_3m,
        'btc_1m_ago':   round(ref, 2),
        'btc_3m_ago':   round(closes[0], 2),
    }


def _event_ts(slug: str):
    m = re.search(r'(\d{10})$', slug)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Leitura dos logs
# ---------------------------------------------------------------------------
sessions = sorted(Path('logs').glob('ee_paper_*/ee_paper.jsonl'))
records = []
for lp in sessions:
    rows = [json.loads(l) for l in lp.read_text(encoding='utf-8').splitlines() if l.strip()]
    entries = {r['slug']: r for r in rows if r.get('type') == 'ee_paper_entry'}
    closed  = {r['slug']: r for r in rows if r.get('type') == 'ee_paper_closed'}
    for slug, entry in entries.items():
        cl = closed.get(slug)
        if not cl:
            continue
        ee = cl.get('ee', {})
        outcome = ee.get('outcome', '?')
        if outcome in ('aberta', 'MISSED'):
            continue
        records.append({
            'slug':    slug,
            'ts':      entry.get('ts', 0),
            'ep':      entry.get('ep', 0),
            'side':    entry.get('side', ''),
            'secs':    entry.get('secs', 0),
            'el_vel':  entry.get('el', {}).get('el_vel', 0),
            'outcome': outcome,
            'pnl':     ee.get('pnl', 0),
        })

n_total = len(records)
print(f"\nTrades carregados: {n_total}")
print("Buscando metricas de momentum BTC (3min antes de cada entrada)...")

n_ok = 0
for r in records:
    mom = _btc_pre_entry(r['ts'], r['side'])
    r.update(mom)
    if mom:
        n_ok += 1

valid = [r for r in records if r.get('delta_1m_usd') is not None]
print(f"  Dados obtidos: {n_ok}/{n_total}\n")

# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------
def _stats(vals):
    if not vals:
        return '   n/a'
    return f"{sum(vals)/len(vals):>+7.1f}"

def _pct(num, den):
    return f"{num/den:>6.1%}" if den else "   n/a"

# ---------------------------------------------------------------------------
# 1. Metricas por outcome
# ---------------------------------------------------------------------------
by_out = defaultdict(list)
for r in valid:
    by_out[r['outcome']].append(r)

print(f"{'='*76}")
print(f"  DIRECAO BTC (delta_1m) E VOLATILIDADE (range_3m) POR OUTCOME")
print(f"{'='*76}")
print(f"  {'Outcome':<16}  {'n':>3}  {'d1m_usd':>8}  {'d1m_bps':>8}  {'d3m_usd':>8}  "
      f"{'d3m_bps':>8}  {'rng3m':>7}  {'adv1m%':>7}")
print("  " + "-"*72)
for out in ['WIN', 'PROFIT_PROTECT', 'STOP_LOSS', 'WIN_HEDGE', 'REVERSAL']:
    grp = by_out.get(out, [])
    if not grp:
        continue
    n = len(grp)
    d1 = [r['delta_1m_usd'] for r in grp]
    d3 = [r['delta_3m_usd'] for r in grp]
    rng = [r['range_3m_usd'] for r in grp]
    adv1 = sum(1 for r in grp if r.get('adverse_1m'))
    print(f"  {out:<16}  {n:>3}  {sum(d1)/n:>+8.1f}  "
          f"{sum(r['delta_1m_bps'] for r in grp)/n:>+8.2f}  "
          f"{sum(d3)/n:>+8.1f}  "
          f"{sum(r['delta_3m_bps'] for r in grp)/n:>+8.2f}  "
          f"{sum(rng)/n:>7.1f}  "
          f"{adv1/n:>7.1%}")

# ---------------------------------------------------------------------------
# 2. Taxa de stop por direcao adversa
# ---------------------------------------------------------------------------
print(f"\n{'='*76}")
print(f"  TAXA DE STOP: ADVERSO vs FAVORAVEL (direcao 1 minuto antes)")
print(f"{'='*76}")
adv   = [r for r in valid if r.get('adverse_1m')]
fav   = [r for r in valid if not r.get('adverse_1m')]
print(f"  {'Grupo':<20}  {'n':>4}  {'WR':>7}  {'STOP%':>7}  {'PnL':>9}  {'avg':>8}")
print("  " + "-"*58)
for label, grp in [('BTC adverso 1m', adv), ('BTC favoravel 1m', fav), ('todos', valid)]:
    if not grp:
        continue
    n     = len(grp)
    wins  = sum(1 for r in grp if r['pnl'] > 0)
    stops = sum(1 for r in grp if r['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(r['pnl'] for r in grp), 2)
    print(f"  {label:<20}  {n:>4}  {wins/n:>7.1%}  {stops/n:>7.1%}  {pnl:>+9.2f}  {pnl/n:>+8.3f}")

adv3  = [r for r in valid if r.get('adverse_3m')]
fav3  = [r for r in valid if not r.get('adverse_3m')]
print(f"\n  {'Grupo':<20}  {'n':>4}  {'WR':>7}  {'STOP%':>7}  {'PnL':>9}  {'avg':>8}")
print("  " + "-"*58)
for label, grp in [('BTC adverso 3m', adv3), ('BTC favoravel 3m', fav3)]:
    if not grp:
        continue
    n     = len(grp)
    wins  = sum(1 for r in grp if r['pnl'] > 0)
    stops = sum(1 for r in grp if r['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(r['pnl'] for r in grp), 2)
    print(f"  {label:<20}  {n:>4}  {wins/n:>7.1%}  {stops/n:>7.1%}  {pnl:>+9.2f}  {pnl/n:>+8.3f}")

# ---------------------------------------------------------------------------
# 3. Gate por volatilidade (range_3m)
# ---------------------------------------------------------------------------
print(f"\n{'='*76}")
print(f"  GATE volatilidade: range_3m_usd < X (nao entrar em BTC muito volatil)")
print(f"{'='*76}")
print(f"  {'Gate rng<':>12}  {'n':>4}  {'WR':>7}  {'STOP%':>7}  {'PnL':>9}  {'avg':>8}  bloq")
print("  " + "-"*64)
base_n = len(valid)
base_pnl = round(sum(r['pnl'] for r in valid), 2)
base_wr = sum(1 for r in valid if r['pnl'] > 0) / base_n
for gate in [50, 80, 100, 120, 150, 200]:
    sub = [r for r in valid if r['range_3m_usd'] < gate]
    if not sub:
        continue
    n     = len(sub)
    wins  = sum(1 for r in sub if r['pnl'] > 0)
    stops = sum(1 for r in sub if r['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(r['pnl'] for r in sub), 2)
    print(f"  {gate:>12}  {n:>4}  {wins/n:>7.1%}  {stops/n:>7.1%}  {pnl:>+9.2f}  {pnl/n:>+8.3f}  ({base_n-n})")
print(f"  {'base':>12}  {base_n:>4}  {base_wr:>7.1%}  "
      f"{sum(1 for r in valid if r['outcome']=='STOP_LOSS')/base_n:>7.1%}  "
      f"{base_pnl:>+9.2f}  {base_pnl/base_n:>+8.3f}")

# ---------------------------------------------------------------------------
# 4. Combinacoes: el_vel + adverso + volatilidade
# ---------------------------------------------------------------------------
print(f"\n{'='*76}")
print(f"  COMBINACOES")
print(f"{'='*76}")
print(f"  {'Cenario':<40}  {'n':>4}  {'WR':>7}  {'STOP%':>7}  {'PnL':>9}  {'avg':>8}")
print("  " + "-"*74)
combos = [
    ('base',                                   lambda r: True),
    ('el_vel>=0.13',                           lambda r: r['el_vel'] >= 0.13),
    ('favoravel_1m',                           lambda r: not r.get('adverse_1m')),
    ('favoravel_3m',                           lambda r: not r.get('adverse_3m')),
    ('range_3m<100',                           lambda r: r['range_3m_usd'] < 100),
    ('el_vel>=0.13 + favoravel_1m',            lambda r: r['el_vel'] >= 0.13 and not r.get('adverse_1m')),
    ('el_vel>=0.13 + favoravel_3m',            lambda r: r['el_vel'] >= 0.13 and not r.get('adverse_3m')),
    ('el_vel>=0.13 + range_3m<100',            lambda r: r['el_vel'] >= 0.13 and r['range_3m_usd'] < 100),
    ('el_vel>=0.12 + favoravel_1m',            lambda r: r['el_vel'] >= 0.12 and not r.get('adverse_1m')),
    ('el_vel>=0.12 + favoravel_3m + rng<100',  lambda r: r['el_vel'] >= 0.12 and not r.get('adverse_3m') and r['range_3m_usd'] < 100),
    ('favoravel_1m + range_3m<100',            lambda r: not r.get('adverse_1m') and r['range_3m_usd'] < 100),
]
for label, fn in combos:
    sub = [r for r in valid if fn(r)]
    if not sub:
        continue
    n     = len(sub)
    wins  = sum(1 for r in sub if r['pnl'] > 0)
    stops = sum(1 for r in sub if r['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(r['pnl'] for r in sub), 2)
    print(f"  {label:<40}  {n:>4}  {wins/n:>7.1%}  {stops/n:>7.1%}  {pnl:>+9.2f}  {pnl/n:>+8.3f}")

# ---------------------------------------------------------------------------
# 5. Detalhe dos STOP_LOSS: direcao e volatilidade
# ---------------------------------------------------------------------------
stops_v = sorted(by_out.get('STOP_LOSS', []), key=lambda r: r['delta_1m_usd'])
print(f"\n  STOP_LOSS — direcao e volatilidade BTC antes da entrada:")
print(f"  {'Slug':>16}  {'ep':>5}  {'d1m':>7}  {'d1m_bps':>7}  {'rng3m':>6}  "
      f"{'adv':>4}  {'el_vel':>6}  {'PnL':>7}")
print("  " + "-"*70)
for r in stops_v:
    adv_str = 'SIM' if r.get('adverse_1m') else 'nao'
    print(f"  {r['slug'][-16:]:>16}  {r['ep']:.3f}  "
          f"{r.get('delta_1m_usd', 0):>+7.1f}  {r.get('delta_1m_bps', 0):>+7.2f}  "
          f"{r.get('range_3m_usd', 0):>6.1f}  {adv_str:>4}  "
          f"{r['el_vel']:>6.3f}  {r['pnl']:>+7.2f}")

# adverso e total
adv_stops = sum(1 for r in by_out.get('STOP_LOSS', []) if r.get('adverse_1m'))
tot_stops = len(by_out.get('STOP_LOSS', []))
print(f"\n  Stops com BTC adverso no 1m antes: {adv_stops}/{tot_stops} "
      f"({adv_stops/tot_stops:.1%})")
adv_wins = sum(1 for r in valid if r.get('adverse_1m') and r['outcome'] != 'STOP_LOSS')
tot_wins = sum(1 for r in valid if r['outcome'] != 'STOP_LOSS')
print(f"  Vencedores com BTC adverso no 1m antes: {adv_wins}/{tot_wins} "
      f"({adv_wins/tot_wins:.1%})")
