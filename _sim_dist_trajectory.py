"""
_sim_dist_trajectory.py
Analisa a TRAJETORIA da distancia BTC->threshold durante o hold.

Para cada trade:
  dist_entry = abs(btc_entry - ptb)   -- ao entrar
  dist_exit  = abs(btc_exit  - ptb)   -- ao sair
  delta      = dist_exit - dist_entry -- positivo = BTC se afastou do threshold
                                         negativo = BTC voltou em direcao ao threshold

Hipotese: STOP_LOSS = delta negativo (BTC voltou)
          WIN/PP    = delta positivo ou estavel
"""
import re
import time
import requests
import statistics
import argparse
from collections import defaultdict
from _ee_log_reader import load_ee_trades

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
TIMEOUT = 6
_cache: dict = {}


def _btc_open_at(ts_unix: float) -> float | None:
    start_ms = int(ts_unix // 60) * 60 * 1000
    key = f"o_{start_ms}"
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
        time.sleep(0.12)
        return val
    except Exception:
        _cache[key] = None
        return None


def _event_ts(slug: str) -> int | None:
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
        'slug':       t['slug'],
        'ts_entry':   t['ts_entry'],
        'ts_exit':    t['ts_exit'],
        'ep':         t['ep'],
        'secs_entry': t['secs'],
        'el_vel':     t['el_vel'],
        'outcome':    t['outcome'],
        'pnl':        t['pnl'],
    })

n_total = len(records)
print(f"\nTrades carregados: {n_total}")
print("Buscando precos Binance historicos (entrada e saida)...")

n_ok = 0
for r in records:
    ev_ts = _event_ts(r['slug'])
    if not ev_ts:
        r.update(ptb=None, btc_entry=None, btc_exit=None,
                 dist_entry=None, dist_exit=None, delta=None)
        continue

    ptb       = _btc_open_at(float(ev_ts))
    btc_entry = _btc_open_at(r['ts_entry'])
    btc_exit  = _btc_open_at(r['ts_exit'])

    if ptb and btc_entry and btc_exit:
        dist_entry = round(abs(btc_entry - ptb), 2)
        dist_exit  = round(abs(btc_exit  - ptb), 2)
        delta      = round(dist_exit - dist_entry, 2)
        n_ok += 1
    else:
        dist_entry = dist_exit = delta = None

    r.update(ptb=ptb, btc_entry=btc_entry, btc_exit=btc_exit,
             dist_entry=dist_entry, dist_exit=dist_exit, delta=delta)

valid = [r for r in records if r['delta'] is not None]
print(f"  Precos obtidos: {n_ok}/{n_total}\n")

# ---------------------------------------------------------------------------
# 1. Trajetoria por outcome
# ---------------------------------------------------------------------------
by_out = defaultdict(list)
for r in valid:
    by_out[r['outcome']].append(r)

print(f"{'='*72}")
print(f"  TRAJETORIA dist_usd: entrada -> saida  (delta = saida - entrada)")
print(f"{'='*72}")
print(f"  {'Outcome':<16}  {'n':>3}  {'dist_ent':>8}  {'dist_sai':>8}  "
      f"{'delta':>7}  {'dir+':>5}  {'dir-':>5}  {'dir0':>5}")
print("  " + "-"*64)

for out in ['WIN', 'PROFIT_PROTECT', 'STOP_LOSS', 'WIN_HEDGE', 'REVERSAL']:
    grp = by_out.get(out, [])
    if not grp:
        continue
    n = len(grp)
    avg_entry = sum(r['dist_entry'] for r in grp) / n
    avg_exit  = sum(r['dist_exit']  for r in grp) / n
    avg_delta = sum(r['delta']      for r in grp) / n
    pos = sum(1 for r in grp if r['delta'] > 5)    # BTC se afastou >$5
    neg = sum(1 for r in grp if r['delta'] < -5)   # BTC voltou >$5
    neu = n - pos - neg                              # variou menos de $5
    print(f"  {out:<16}  {n:>3}  {avg_entry:>8.1f}  {avg_exit:>8.1f}  "
          f"{avg_delta:>+7.1f}  {pos:>5}  {neg:>5}  {neu:>5}")

# ---------------------------------------------------------------------------
# 2. Distribuicao do delta por outcome
# ---------------------------------------------------------------------------
print(f"\n{'='*72}")
print(f"  DELTA dist_usd por outcome  (positivo = BTC se afastou, negativo = BTC voltou)")
print(f"{'='*72}")
print(f"  {'Outcome':<16}  {'n':>3}  {'avg':>7}  {'med':>7}  {'min':>7}  {'max':>7}  "
      f"{'delta<-20':>9}  {'delta<0':>7}  {'delta>0':>7}  {'delta>20':>8}")
print("  " + "-"*78)

for out in ['WIN', 'PROFIT_PROTECT', 'STOP_LOSS', 'WIN_HEDGE', 'REVERSAL']:
    grp = by_out.get(out, [])
    if not grp:
        continue
    deltas = [r['delta'] for r in grp]
    n = len(deltas)
    print(f"  {out:<16}  {n:>3}  {sum(deltas)/n:>+7.1f}  "
          f"{statistics.median(deltas):>+7.1f}  {min(deltas):>+7.1f}  {max(deltas):>+7.1f}  "
          f"{sum(1 for d in deltas if d < -20):>9}  "
          f"{sum(1 for d in deltas if d < 0):>7}  "
          f"{sum(1 for d in deltas if d > 0):>7}  "
          f"{sum(1 for d in deltas if d > 20):>8}")

# ---------------------------------------------------------------------------
# 3. Detalhe dos STOP_LOSS: trajetoria individual
# ---------------------------------------------------------------------------
stops = sorted(by_out.get('STOP_LOSS', []), key=lambda r: r['delta'])
print(f"\n  STOP_LOSS — trajetoria individual (do maior recuo ao menor):")
print(f"  {'Slug':>16}  {'ep':>5}  {'dist_ent':>8}  {'dist_sai':>8}  {'delta':>7}  "
      f"{'el_vel':>6}  {'secs':>5}  {'PnL':>7}")
print("  " + "-"*72)
for r in stops:
    print(f"  {r['slug'][-16:]:>16}  {r['ep']:.3f}  "
          f"{r['dist_entry']:>8.1f}  {r['dist_exit']:>8.1f}  {r['delta']:>+7.1f}  "
          f"{r['el_vel']:>6.3f}  {r['secs_entry']:>5}  {r['pnl']:>+7.2f}")

# ---------------------------------------------------------------------------
# 4. Gate baseado no delta (pos-hoc — apenas para entender o padrao)
# ---------------------------------------------------------------------------
print(f"\n{'='*72}")
print(f"  RESUMO: delta medio por outcome e por gate el_vel")
print(f"{'='*72}")
for label, fn in [
    ('todos', lambda r: True),
    ('el_vel >= 0.13', lambda r: r['el_vel'] >= 0.13),
    ('el_vel < 0.13',  lambda r: r['el_vel'] < 0.13),
]:
    sub = [r for r in valid if fn(r)]
    if not sub:
        continue
    by = defaultdict(list)
    for r in sub:
        by[r['outcome']].append(r['delta'])
    parts = []
    for out in ['WIN', 'PROFIT_PROTECT', 'STOP_LOSS']:
        vals = by.get(out, [])
        if vals:
            parts.append(f"{out}: avg delta={sum(vals)/len(vals):>+.1f} (n={len(vals)})")
    print(f"\n  [{label}]")
    for p in parts:
        print(f"    {p}")
