#!/usr/bin/env python3
"""
_backtest_bid_prices.py

Backtest dos setups EE e AR com bid prices reais (best_bid do orderbook BrockMisner).
Granularidade: ~1s por snapshot após resample DuckDB (vs 500ms do bot real).
Periodo: fev-abr 2026  |  616 mercados com resolucao direta.

Diferença do backtest anterior (_backtest_brockMisner.py):
- Usa best_bid (orderbook) em vez de up_price (mid-price)
- bid ≈ mid - 0.005~0.01: thresholds comparáveis ao bot real
- Resample 1s via DuckDB: n_s180 recalibrado para n_s180 < 30
  (bot real a 0.5s: ~120 snaps em 60s; dataset 1s: ~60 snaps em 60s)
"""
import sys, warnings
warnings.filterwarnings('ignore')
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

import numpy as np
import pandas as pd
from scipy.stats import binomtest

# ── Carregar dados ────────────────────────────────────────────────────────────
print("Carregando orderbook filtrado...")
ob  = pd.read_parquet('_btc5_ob_filtered.parquet')
res = pd.read_parquet('_btc5_res_map.parquet')
mkt = pd.read_parquet('_btc5_markets.parquet')

print(f"Orderbook: {len(ob):,} linhas  |  mercados: {ob['market_id'].nunique():,}")
print(f"Resolucoes: {len(res):,}")

# Verifica estrutura — DuckDB salvou ts_sec (segundos, não ms)
print(f"outcome values: {ob['outcome'].unique()}")
print(f"best_bid range: {ob['best_bid'].min():.3f} a {ob['best_bid'].max():.3f}")
print(f"ts_sec sample: {ob['ts_sec'].head(2).tolist()}")

# Monta resolution map
res_map = dict(zip(res['market_id'].astype(str), res['resolution'].astype(int)))

# Merge end_ts para calcular secs
mkt['market_id'] = mkt['market_id'].astype(str)
ob = ob.merge(mkt[['market_id', 'end_ts']], on='market_id', how='left')

# secs = segundos restantes (ts_sec já está em segundos)
ob['secs'] = (ob['end_ts'] - ob['ts_sec']).clip(0, 360).astype(float)

# Pivot: uma linha por (ts_sec, market_id) com up_bid e down_bid separados
ob_up   = ob[ob['outcome'] == 'Up'  ][['ts_sec','market_id','secs','best_bid']].rename(columns={'best_bid':'up_bid'})
ob_down = ob[ob['outcome'] == 'Down'][['ts_sec','market_id','secs','best_bid']].rename(columns={'best_bid':'down_bid'})
snaps   = ob_up.merge(ob_down, on=['ts_sec','market_id','secs'], how='inner')
snaps   = snaps.sort_values(['market_id', 'ts_sec'])

print(f"\nSnaps pivotados (up_bid + down_bid por ts_sec): {len(snaps):,}")
snaps_per_mkt = snaps.groupby('market_id').size()
print(f"Snaps/mercado: mean={snaps_per_mkt.mean():.0f}  median={snaps_per_mkt.median():.0f}  max={snaps_per_mkt.max():.0f}")

# n_s180 recalibrado: dataset ~1s/snap (bot real 0.5s)
# bot: ~120 snaps em 60s -> gate n_s180<6 exige >=6 snaps
# dataset: ~60 snaps em 60s -> gate equivalente: n_s180>=3

# ── Constantes EE (do live_early_entry_paper_v1.py) ──────────────────────────
EE_EL_MIN   = 0.55
EE_CONT_MIN = 0.70
EE_VEL_MIN  = 0.17
EE_ENTRY_LO = 0.82
EE_ENTRY_HI = 0.85
EE_MIN_SECS = 30
EE_MAX_SECS = 180

# n_s180 gate recalibrado para granularidade ~100ms:
# bot real 0.5s: ~120 snaps/min → gate n_s180<6 bloqueia se <6 snaps
# dataset 100ms: ~600 snaps/min → gate equivalente: n_s180<30
# Testamos ambos: com gate (n_s180>=30) e sem (controle)
N180_GATE_BID = 3   # ~60 snaps/60s no dataset 1s (bot real: 120 snaps -> gate=6)

def run_ee(df_markets, use_n180_gate: bool, label: str) -> pd.DataFrame:
    results = []
    for mkt_id, grp in df_markets.groupby('market_id'):
        if mkt_id not in res_map:
            continue
        resolution = res_map[mkt_id]
        rows = grp[['secs', 'up_bid', 'down_bid']].values

        s240, s180 = [], []
        el_side = None
        el_bid_240 = el_bid_180 = el_vel = 0.0
        cont_ok = False
        entered = False

        for secs, up_bid, dn_bid in rows:
            secs = float(secs)
            if 181 <= secs <= 240:
                s240.append((up_bid, dn_bid))
            elif 121 <= secs <= 180:
                s180.append((up_bid, dn_bid))
                if s240 and el_side is None:
                    avg_up = float(np.mean([x[0] for x in s240]))
                    avg_dn = float(np.mean([x[1] for x in s240]))
                    if avg_up >= EE_EL_MIN:
                        el_side, el_bid_240 = 'UP', avg_up
                    elif avg_dn >= EE_EL_MIN:
                        el_side, el_bid_240 = 'DOWN', avg_dn
                if el_side:
                    bids = [x[0] if el_side == 'UP' else x[1] for x in s180]
                    el_bid_180 = float(np.mean(bids))
                    el_vel     = round(el_bid_180 - el_bid_240, 4)
                    cont_ok    = float(min(bids)) >= EE_CONT_MIN
            elif EE_MIN_SECS <= secs <= EE_MAX_SECS and not entered:
                if not el_side:
                    continue
                el_bid = up_bid if el_side == 'UP' else dn_bid
                n180   = len(s180)
                signal = (cont_ok
                          and el_vel >= EE_VEL_MIN
                          and EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI)
                gate_ok = (n180 >= N180_GATE_BID) if use_n180_gate else True
                if signal and gate_ok:
                    win = (el_side == 'UP' and resolution == 1) or \
                          (el_side == 'DOWN' and resolution == 0)
                    results.append({
                        'market_id':   mkt_id,
                        'entry_secs':  int(secs),
                        'entry_price': round(el_bid, 4),
                        'el_vel':      el_vel,
                        'n_s180':      n180,
                        'el_side':     el_side,
                        'resolution':  resolution,
                        'win':         win,
                    })
                    entered = True
    return pd.DataFrame(results)


def run_ar(df_markets, threshold: float = 0.88, max_secs: int = 60) -> pd.DataFrame:
    results = []
    for mkt_id, grp in df_markets.groupby('market_id'):
        if mkt_id not in res_map:
            continue
        resolution = res_map[mkt_id]
        rows = grp[['secs', 'up_bid', 'down_bid']].values
        entered = False
        for secs, up_bid, dn_bid in rows:
            if float(secs) > max_secs or entered:
                continue
            for side, bid in [('UP', up_bid), ('DOWN', dn_bid)]:
                if bid >= threshold:
                    win = (side == 'UP' and resolution == 1) or \
                          (side == 'DOWN' and resolution == 0)
                    results.append({
                        'market_id': mkt_id, 'entry_secs': int(secs),
                        'entry_price': round(bid, 4),
                        'side': side, 'resolution': resolution, 'win': win,
                    })
                    entered = True
                    break
    return pd.DataFrame(results)


# ── Rodar backtests ───────────────────────────────────────────────────────────
print("\nRodando EE com bid prices...")
df_ee     = run_ee(snaps, use_n180_gate=True,  label='EE com gate n180>=30')
df_ee_ng  = run_ee(snaps, use_n180_gate=False, label='EE sem gate n180')
print(f"EE com gate: {len(df_ee)}  |  EE sem gate: {len(df_ee_ng)}")

print("Rodando AR com bid prices...")
df_ar     = run_ar(snaps, threshold=0.88, max_secs=60)
df_ar_75  = run_ar(snaps, threshold=0.75, max_secs=120)
print(f"AR >=0.88 secs<=60: {len(df_ar)}  |  AR >=0.75 secs<=120: {len(df_ar_75)}")


# ── Relatório ─────────────────────────────────────────────────────────────────
def wilson_ci(k, n, z=1.96):
    p = k / n
    lo = (p + z**2/(2*n) - z*(p*(1-p)/n + z**2/(4*n**2))**0.5) / (1+z**2/n)
    hi = (p + z**2/(2*n) + z*(p*(1-p)/n + z**2/(4*n**2))**0.5) / (1+z**2/n)
    return lo, hi

def report(df, label, vel_col='el_vel'):
    print(f"\n{'─'*58}")
    print(f"  {label}")
    print(f"{'─'*58}")
    if df.empty:
        print("  0 entradas encontradas.")
        return
    n  = len(df)
    k  = int(df['win'].sum())
    wr = k / n
    ep = df['entry_price'].mean()
    lo, hi = wilson_ci(k, n)
    bt = binomtest(k, n, 0.5, alternative='greater')
    sig = 'SIGNIFICATIVO' if bt.pvalue < 0.01 else 'nao sig'
    print(f"  n={n}  wins={k}  WR={wr:.1%}  IC95%=[{lo:.1%},{hi:.1%}]")
    print(f"  EP medio={ep:.4f}  p-valor={bt.pvalue:.2e} ({sig})")
    print(f"  Mercados distintos: {df['market_id'].nunique()}")

    if vel_col in df.columns:
        print(f"\n  WR por el_vel:")
        for lo_v, hi_v in [(0.17,0.22),(0.22,0.30),(0.30,0.45),(0.45,1.0)]:
            sub = df[(df[vel_col]>=lo_v) & (df[vel_col]<hi_v)]
            if len(sub):
                kk = int(sub['win'].sum())
                print(f"    [{lo_v:.2f},{hi_v:.2f}): n={len(sub):>4}  WR={kk/len(sub):.1%}  ep={sub['entry_price'].mean():.4f}")

        print(f"\n  WR por entry_price (bid real):")
        for lo_e, hi_e in [(0.82,0.83),(0.83,0.84),(0.84,0.85),(0.85,0.86)]:
            sub = df[(df['entry_price']>=lo_e) & (df['entry_price']<hi_e)]
            if len(sub):
                kk = int(sub['win'].sum())
                print(f"    [{lo_e:.2f},{hi_e:.2f}): n={len(sub):>4}  WR={kk/len(sub):.1%}")

        print(f"\n  WR por entry_secs:")
        for lo_s, hi_s in [(30,60),(61,90),(91,120),(121,180)]:
            sub = df[(df['entry_secs']>=lo_s) & (df['entry_secs']<=hi_s)]
            if len(sub):
                kk = int(sub['win'].sum())
                print(f"    [{lo_s}-{hi_s}s): n={len(sub):>4}  WR={kk/len(sub):.1%}")

    if 'side' in df.columns and vel_col not in df.columns:
        print(f"\n  WR por faixa de bid (AR):")
        for lo_e, hi_e, lbl in [(0.75,0.80,'0.75-0.80'),(0.80,0.85,'0.80-0.85'),
                                  (0.85,0.90,'0.85-0.90'),(0.90,0.95,'0.90-0.95'),(0.95,1.01,'>0.95')]:
            sub = df[(df['entry_price']>=lo_e) & (df['entry_price']<hi_e)]
            if len(sub):
                kk = int(sub['win'].sum())
                print(f"    {lbl}: n={len(sub):>4}  WR={kk/len(sub):.1%}")


print(f"\n{'='*58}")
print(f"  BACKTEST BID PRICES — BrockMisner BTC 5min")
print(f"  Periodo fev-abr 2026  |  Granularidade ~100ms")
print(f"{'='*58}")
print(f"  Mercados no orderbook: {snaps['market_id'].nunique():,}")
print(f"  Com resolucao valida : {len(res_map):,}")

report(df_ee,    f"EE  gate n180>={N180_GATE_BID}  (bid real)")
report(df_ee_ng, f"EE  sem gate n180  (bid real, controle)")
report(df_ar,    "AR  bid>=0.88  secs<=60  (bid real)")
report(df_ar_75, "AR  bid>=0.75  secs<=120  (bid real, janela maior)")

# Comparacao com backtest mid-price
print(f"\n{'─'*58}")
print("  COMPARACAO: mid-price vs bid real")
print(f"{'─'*58}")
try:
    old = pd.read_csv('_result_ee.csv')
    print(f"  Mid-price (anterior): n={len(old)}  WR={old['win'].mean():.1%}  ep={old['entry_price'].mean():.4f}")
except: pass
if not df_ee_ng.empty:
    print(f"  Bid real  (este):     n={len(df_ee_ng)}  WR={df_ee_ng['win'].mean():.1%}  ep={df_ee_ng['entry_price'].mean():.4f}")

# Salva resultados
df_ee.to_csv('_result_ee_bid.csv', index=False)
df_ee_ng.to_csv('_result_ee_bid_ng.csv', index=False)
df_ar.to_csv('_result_ar_bid.csv', index=False)
df_ar_75.to_csv('_result_ar_bid_75.csv', index=False)
print(f"\nResultados salvos: _result_ee_bid.csv, _result_ar_bid.csv")
