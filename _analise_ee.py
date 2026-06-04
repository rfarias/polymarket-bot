import warnings; warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np
from scipy.stats import binomtest

df  = pd.read_csv('_result_ee.csv')
mkt = pd.read_parquet('_btc5_markets.parquet')

print('=== VALIDACAO DOS GATES EE ===')
n = len(df); k = int(df['win'].sum())
p_hat = k / n
z = 1.96
lo = (p_hat + z**2/(2*n) - z*(p_hat*(1-p_hat)/n + z**2/(4*n**2))**0.5) / (1+z**2/n)
hi = (p_hat + z**2/(2*n) + z*(p_hat*(1-p_hat)/n + z**2/(4*n**2))**0.5) / (1+z**2/n)
res = binomtest(k, n, 0.5, alternative='greater')
sig = 'SIGNIFICATIVO' if res.pvalue < 0.01 else 'nao sig'
print(f'n={n}  wins={k}  WR={p_hat:.1%}  IC95%=[{lo:.1%},{hi:.1%}]')
print(f'p-valor vs 50/50: {res.pvalue:.2e}  ({sig})')

n_days = (pd.to_datetime(mkt['end_ts'],unit='s').max() - pd.to_datetime(mkt['start_ts'],unit='s').min()).days
print(f'Periodo: {n_days} dias | 5817 resolvidos | sinal EE: {n/5817:.1%} dos mkts ({n/n_days:.1f}/dia)')

# Sem gate n180 — precisa rodar o backtest no modo sem gate
# Carrega _result_ee_ng se existir
try:
    df_ng = pd.read_csv('_result_ee_ng.csv')
    n2 = len(df_ng); k2 = int(df_ng['win'].sum())
    print(f'\nSem gate: n={n2} WR={k2/n2:.1%}')
except: pass

# Distribuicao de vel_min abaixo do threshold
print('\nWR por faixa de el_vel (entradas com gate n180>=2):')
for lo_v, hi_v, lbl in [(0.17,0.22,'0.17-0.22'),(0.22,0.30,'0.22-0.30'),(0.30,1.0,'>0.30')]:
    sub = df[(df['el_vel']>=lo_v)&(df['el_vel']<hi_v)]
    if len(sub):
        print(f'  vel {lbl}: n={len(sub):>3}  WR={sub["win"].mean():.1%}  ep_med={sub["entry_price"].mean():.3f}')

print('\nWR por entry_secs:')
for lo_s, hi_s, lbl in [(30,60,'30-60'),(61,90,'61-90'),(91,120,'91-120'),(121,180,'121-180')]:
    sub = df[(df['entry_secs']>=lo_s)&(df['entry_secs']<=hi_s)]
    if len(sub):
        print(f'  secs {lbl}: n={len(sub):>3}  WR={sub["win"].mean():.1%}')
