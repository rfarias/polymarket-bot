"""
_sim_dist_ptb_retro.py
Reconstroi a distancia BTC spot -> price to beat para os trades historicos.

O slug "btc-updown-5m-XXXXXXXXXX" contem o Unix timestamp do inicio do candle
(= price to beat = abertura do candle Binance naquele instante).
O timestamp da entrada (ts) permite buscar o BTC spot naquele minuto.

Campos calculados por trade:
  ptb        -- price to beat (abertura do candle Binance)
  btc_entry  -- BTC spot no minuto da entrada
  dist_usd   -- abs(btc_entry - ptb)
  dist_bps   -- dist_usd / ptb * 10000
  secs       -- segundos restantes na entrada
"""
import re
import time
import requests
import argparse
from collections import defaultdict
from _ee_log_reader import load_ee_trades

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
TIMEOUT = 6

# Cache para evitar chamadas repetidas a Binance
_cache: dict = {}


def _binance_1m_open(ts_unix: float) -> float | None:
    """Retorna o preco de abertura do minuto 1m que contem ts_unix."""
    start_ms = int(ts_unix // 60) * 60 * 1000
    key = f"open_{start_ms}"
    if key in _cache:
        return _cache[key]
    try:
        r = requests.get(BINANCE_KLINES, params={
            "symbol": "BTCUSDT", "interval": "1m",
            "startTime": start_ms, "limit": 1,
        }, timeout=TIMEOUT)
        data = r.json() or []
        row = data[0] if data else None
        val = float(row[1]) if row and len(row) > 1 else None
        _cache[key] = val
        time.sleep(0.15)   # respeitar rate limit
        return val
    except Exception:
        _cache[key] = None
        return None


def _extract_event_ts(slug: str) -> int | None:
    """Extrai o Unix timestamp do inicio do candle a partir do slug."""
    m = re.search(r'(\d{10})$', slug)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Leitura dos logs
# ---------------------------------------------------------------------------
_ap = argparse.ArgumentParser()
_ap.add_argument('--logs', default='logs/ee_paper_*/ee_paper.jsonl',
                 help='Glob pattern para logs EE (paper ou real)')
_args = _ap.parse_args()

records = []
for t in load_ee_trades(_args.logs):
    records.append({
        'slug':    t['slug'],
        'ts':      t['ts_entry'],
        'ep':      t['ep'],
        'side':    t['side'],
        'secs':    t['secs'],
        'el_vel':  t['el_vel'],
        'outcome': t['outcome'],
        'pnl':     t['pnl'],
    })

n_total = len(records)
print(f"\nTrades carregados: {n_total}")

# ---------------------------------------------------------------------------
# Busca precos Binance historicos
# ---------------------------------------------------------------------------
print("Buscando precos Binance historicos (pode levar ~30s)...")
print("  slug  |  event_start  |  ptb  |  btc_entry  |  dist_usd")

n_ok = 0
for r in records:
    event_ts = _extract_event_ts(r['slug'])
    if not event_ts:
        r['ptb'] = None
        r['btc_entry'] = None
        r['dist_usd'] = None
        r['dist_bps'] = None
        continue

    ptb       = _binance_1m_open(float(event_ts))    # preco de abertura do candle
    btc_entry = _binance_1m_open(r['ts'])             # btc no minuto da entrada

    if ptb and btc_entry:
        dist_usd = round(abs(btc_entry - ptb), 2)
        dist_bps = round(dist_usd / ptb * 10000, 2)
        n_ok += 1
    else:
        dist_usd = None
        dist_bps = None

    r['ptb']       = ptb
    r['btc_entry'] = btc_entry
    r['dist_usd']  = dist_usd
    r['dist_bps']  = dist_bps

valid = [r for r in records if r['dist_usd'] is not None]
print(f"\n  Precos obtidos: {n_ok}/{n_total}")

# ---------------------------------------------------------------------------
# 1. Distribuicao dist_usd por outcome
# ---------------------------------------------------------------------------
import statistics

by_out = defaultdict(list)
for r in valid:
    by_out[r['outcome']].append(r['dist_usd'])

print(f"\n{'='*70}")
print(f"  DIST_PTB_USD POR OUTCOME  ({len(valid)} trades com dados)")
print(f"{'='*70}")
print(f"  {'Outcome':<16}  {'n':>3}  {'avg':>7}  {'med':>7}  {'min':>7}  {'max':>7}")
print("  " + "-"*52)
for out in ['WIN', 'PROFIT_PROTECT', 'STOP_LOSS', 'WIN_HEDGE', 'REVERSAL']:
    vals = by_out.get(out, [])
    if not vals:
        continue
    print(f"  {out:<16}  {len(vals):>3}  {sum(vals)/len(vals):>7.1f}  "
          f"{statistics.median(vals):>7.1f}  {min(vals):>7.1f}  {max(vals):>7.1f}")

# ---------------------------------------------------------------------------
# 2. Distribuicao dist_bps por outcome
# ---------------------------------------------------------------------------
by_out_bps = defaultdict(list)
for r in valid:
    by_out_bps[r['outcome']].append(r['dist_bps'])

print(f"\n  DIST_PTB_BPS POR OUTCOME:")
print(f"  {'Outcome':<16}  {'n':>3}  {'avg':>7}  {'med':>7}  {'min':>7}  {'max':>7}")
print("  " + "-"*52)
for out in ['WIN', 'PROFIT_PROTECT', 'STOP_LOSS', 'WIN_HEDGE', 'REVERSAL']:
    vals = by_out_bps.get(out, [])
    if not vals:
        continue
    print(f"  {out:<16}  {len(vals):>3}  {sum(vals)/len(vals):>7.1f}  "
          f"{statistics.median(vals):>7.1f}  {min(vals):>7.1f}  {max(vals):>7.1f}")

# ---------------------------------------------------------------------------
# 3. Gate dist_usd < X (bloquear sobreextensao — direcao util)
# ---------------------------------------------------------------------------
print(f"\n{'='*70}")
print(f"  GATE dist_ptb_usd < X  (bloquear BTC sobreestendido)")
print(f"{'='*70}")
print(f"  {'Gate usd<':>10}  {'n':>4}  {'WR':>7}  {'STOP%':>6}  {'PnL':>8}  {'avg':>7}  bloq")
print("  " + "-"*60)
base_n   = len(valid)
base_pnl = round(sum(r['pnl'] for r in valid), 2)
base_wr  = sum(1 for r in valid if r['pnl'] > 0) / base_n if base_n else 0
for gate in [20, 30, 40, 50, 60, 80, 100]:
    sub = [r for r in valid if r['dist_usd'] < gate]
    if not sub:
        continue
    n     = len(sub)
    wins  = sum(1 for r in sub if r['pnl'] > 0)
    stops = sum(1 for r in sub if r['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(r['pnl'] for r in sub), 2)
    blk   = base_n - n
    print(f"  {gate:>10}  {n:>4}  {wins/n:>7.1%}  {stops/n:>6.1%}  {pnl:>+8.2f}  {pnl/n:>+7.3f}  ({blk})")
print(f"  {'base':>10}  {base_n:>4}  {base_wr:>7.1%}  "
      f"{sum(1 for r in valid if r['outcome']=='STOP_LOSS')/base_n:>6.1%}  "
      f"{base_pnl:>+8.2f}  {base_pnl/base_n:>+7.3f}")

# ---------------------------------------------------------------------------
# 4. Gate dist_bps < X (mesma logica — bloquear sobreextensao em bps)
# ---------------------------------------------------------------------------
print(f"\n{'='*70}")
print(f"  GATE dist_ptb_bps < X  (bloquear BTC sobreestendido em bps)")
print(f"{'='*70}")
print(f"  {'Gate bps<':>10}  {'n':>4}  {'WR':>7}  {'STOP%':>6}  {'PnL':>8}  {'avg':>7}  bloq")
print("  " + "-"*60)
for gate in [3, 5, 8, 10, 15, 20]:
    sub = [r for r in valid if r['dist_bps'] < gate]
    if not sub:
        continue
    n     = len(sub)
    wins  = sum(1 for r in sub if r['pnl'] > 0)
    stops = sum(1 for r in sub if r['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(r['pnl'] for r in sub), 2)
    blk   = base_n - n
    print(f"  {gate:>10}  {n:>4}  {wins/n:>7.1%}  {stops/n:>6.1%}  {pnl:>+8.2f}  {pnl/n:>+7.3f}  ({blk})")

# ---------------------------------------------------------------------------
# 5. Combinacoes: el_vel + dist_usd
# ---------------------------------------------------------------------------
print(f"\n{'='*70}")
print(f"  COMBINACOES el_vel + dist_usd")
print(f"{'='*70}")
print(f"  {'Cenario':<35}  {'n':>4}  {'WR':>7}  {'STOP%':>6}  {'PnL':>8}  {'avg':>7}")
print("  " + "-"*68)
combos = [
    ('base',                          lambda r: True),
    ('el_vel>=0.13',                  lambda r: r['el_vel'] >= 0.13),
    ('dist_usd<30',                   lambda r: r['dist_usd'] < 30),
    ('dist_usd<50',                   lambda r: r['dist_usd'] < 50),
    ('dist_usd<80',                   lambda r: r['dist_usd'] < 80),
    ('el_vel>=0.13 + dist_usd<50',   lambda r: r['el_vel'] >= 0.13 and r['dist_usd'] < 50),
    ('el_vel>=0.13 + dist_usd<80',   lambda r: r['el_vel'] >= 0.13 and r['dist_usd'] < 80),
    ('el_vel>=0.12 + dist_usd<50',   lambda r: r['el_vel'] >= 0.12 and r['dist_usd'] < 50),
    ('el_vel>=0.12 + dist_usd<80',   lambda r: r['el_vel'] >= 0.12 and r['dist_usd'] < 80),
    ('el_vel>=0.13 + dist_usd>=50',  lambda r: r['el_vel'] >= 0.13 and r['dist_usd'] >= 50),
]
for label, fn in combos:
    sub = [r for r in valid if fn(r)]
    if not sub:
        continue
    n     = len(sub)
    wins  = sum(1 for r in sub if r['pnl'] > 0)
    stops = sum(1 for r in sub if r['outcome'] == 'STOP_LOSS')
    pnl   = round(sum(r['pnl'] for r in sub), 2)
    print(f"  {label:<35}  {n:>4}  {wins/n:>7.1%}  {stops/n:>6.1%}  {pnl:>+8.2f}  {pnl/n:>+7.3f}")

# ---------------------------------------------------------------------------
# 6. Detalhe dos STOP_LOSS — dist_usd, dist_bps, secs
# ---------------------------------------------------------------------------
stops_v = sorted([r for r in valid if r['outcome'] == 'STOP_LOSS'],
                 key=lambda r: r['dist_usd'])
print(f"\n  STOP_LOSS — dist_usd, dist_bps e secs na entrada (do mais proximo ao mais distante):")
print(f"  {'Slug':>16}  {'ep':>5}  {'dist_usd':>8}  {'dist_bps':>8}  {'secs':>5}  {'el_vel':>6}  {'PnL':>7}")
print("  " + "-"*65)
for r in stops_v:
    print(f"  {r['slug'][-16:]:>16}  {r['ep']:.3f}  "
          f"{r['dist_usd']:>8.1f}  {r['dist_bps']:>8.2f}  "
          f"{r['secs']:>5}  {r['el_vel']:>6.3f}  {r['pnl']:>+7.2f}")

if stops_v:
    avg_dist_stop = sum(r['dist_usd'] for r in stops_v) / len(stops_v)
    wins_v = [r for r in valid if r['outcome'] != 'STOP_LOSS']
    avg_dist_win  = sum(r['dist_usd'] for r in wins_v) / len(wins_v) if wins_v else 0
    print(f"\n  dist_usd medio:  stops={avg_dist_stop:.1f}  nao-stops={avg_dist_win:.1f}  diff={avg_dist_stop-avg_dist_win:+.1f}")
