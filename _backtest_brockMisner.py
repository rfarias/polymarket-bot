#!/usr/bin/env python3
"""
_backtest_brockMisner.py

Backtest dos setups EE e AR contra BrockMisner/polymarket-btc-updown.
Dados: mid-price (up_price/down_price) -- bid real ≈ mid - 0.01.
Os thresholds são aplicados diretamente no mid-price, o que é
ligeiramente mais conservador que no bot real (bid < mid).
"""
import sys, warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
from collections import defaultdict

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ── Carregar dados ────────────────────────────────────────────────────────────
print("Carregando dados...")
mkt = pd.read_parquet('_btc5_markets.parquet')
p5  = pd.read_parquet('_btc5_prices.parquet')

# Merge secs
p5 = p5.merge(mkt[['market_id','start_ts','end_ts','resolution']], on='market_id', how='left')
p5['secs'] = (p5['end_ts'] - p5['timestamp']).clip(0, 360)

# Resolução: usa campo direto (1=UP,0=DOWN) ou infere do último preço
res_map: dict[str, int] = {}
for _, r in mkt.iterrows():
    if r['resolution'] in (0, 1):
        res_map[r['market_id']] = int(r['resolution'])

last = p5.sort_values('timestamp').groupby('market_id').last()
for mid, row in last.iterrows():
    if mid not in res_map:
        if row['up_price'] > 0.90:
            res_map[mid] = 1
        elif row['up_price'] < 0.10:
            res_map[mid] = 0

resolved_ids = set(res_map.keys())
p5_res = p5[p5['market_id'].isin(resolved_ids)].copy()
print(f"Mercados com resolução conhecida: {len(resolved_ids):,}  "
      f"(snapshots: {len(p5_res):,})")

# ── Constantes EE (do live_early_entry_paper_v1.py) ──────────────────────────
EE_EL_MIN   = 0.55   # bid mín para detectar EL em secs 181-240
EE_CONT_MIN = 0.70   # bid mín de continuidade em secs 121-180
EE_VEL_MIN  = 0.17   # crescimento mínimo bid_180 - bid_240
EE_ENTRY_LO = 0.82   # faixa de entrada: mínimo
EE_ENTRY_HI = 0.85   # faixa de entrada: máximo
EE_MIN_SECS = 30
EE_MAX_SECS = 180

# n_s180 gate recalibrado:
# Dataset tem ~5 snaps por minuto vs bot real 0.5s = ~120/min.
# Gate original: n_s180 < 6 (exige dados suficientes na janela).
# Aqui equivalente: n_s180 < 2.
N180_GATE = 2

# ── Backtest EE ───────────────────────────────────────────────────────────────
print("\nRodando backtest EE...")
results_ee = []

for mkt_id, grp in p5_res.groupby('market_id'):
    resolution = res_map[mkt_id]
    rows = grp.sort_values('timestamp')[['secs','up_price','down_price']].values

    s240, s180 = [], []
    el_side = None
    el_bid_240 = el_bid_180 = el_vel = 0.0
    cont_ok = False
    entered = False

    for secs, up_mid, dn_mid in rows:
        secs = int(secs)

        # Janela EL inicial (secs 181-240)
        if 181 <= secs <= 240:
            s240.append((up_mid, dn_mid))

        # Janela de continuidade (secs 121-180)
        elif 121 <= secs <= 180:
            s180.append((up_mid, dn_mid))

            # Detecta EL ao entrar na janela 121-180
            if s240 and el_side is None:
                avg_up = np.mean([x[0] for x in s240])
                avg_dn = np.mean([x[1] for x in s240])
                if avg_up >= EE_EL_MIN:
                    el_side, el_bid_240 = 'UP', avg_up
                elif avg_dn >= EE_EL_MIN:
                    el_side, el_bid_240 = 'DOWN', avg_dn

            # Atualiza el_bid_180 e el_vel a cada snap
            if el_side:
                bids = [x[0] if el_side == 'UP' else x[1] for x in s180]
                el_bid_180 = float(np.mean(bids))
                el_vel     = round(el_bid_180 - el_bid_240, 4)
                cont_ok    = float(min(bids)) >= EE_CONT_MIN

        # Janela de entrada (secs 30-180)
        elif EE_MIN_SECS <= secs <= EE_MAX_SECS and not entered:
            if not el_side:
                continue
            el_bid = up_mid if el_side == 'UP' else dn_mid
            n180   = len(s180)

            signal = (
                cont_ok
                and el_vel >= EE_VEL_MIN
                and EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI
            )
            if signal and n180 >= N180_GATE:
                win = (el_side == 'UP' and resolution == 1) or \
                      (el_side == 'DOWN' and resolution == 0)
                results_ee.append({
                    'market_id':   mkt_id,
                    'entry_secs':  secs,
                    'entry_price': round(el_bid, 4),
                    'el_vel':      el_vel,
                    'n_s180':      n180,
                    'el_side':     el_side,
                    'resolution':  resolution,
                    'win':         win,
                })
                entered = True

df_ee = pd.DataFrame(results_ee)

# ── Backtest EE — sem gate n_s180 (controle) ─────────────────────────────────
results_ee_ng = []
for mkt_id, grp in p5_res.groupby('market_id'):
    resolution = res_map[mkt_id]
    rows = grp.sort_values('timestamp')[['secs','up_price','down_price']].values
    s240, s180 = [], []
    el_side = None
    el_bid_240 = el_bid_180 = el_vel = 0.0
    cont_ok = False
    entered = False
    for secs, up_mid, dn_mid in rows:
        secs = int(secs)
        if 181 <= secs <= 240:
            s240.append((up_mid, dn_mid))
        elif 121 <= secs <= 180:
            s180.append((up_mid, dn_mid))
            if s240 and el_side is None:
                avg_up = np.mean([x[0] for x in s240])
                avg_dn = np.mean([x[1] for x in s240])
                if avg_up >= EE_EL_MIN: el_side, el_bid_240 = 'UP', avg_up
                elif avg_dn >= EE_EL_MIN: el_side, el_bid_240 = 'DOWN', avg_dn
            if el_side:
                bids = [x[0] if el_side == 'UP' else x[1] for x in s180]
                el_bid_180 = float(np.mean(bids))
                el_vel  = round(el_bid_180 - el_bid_240, 4)
                cont_ok = float(min(bids)) >= EE_CONT_MIN
        elif EE_MIN_SECS <= secs <= EE_MAX_SECS and not entered:
            if not el_side: continue
            el_bid = up_mid if el_side == 'UP' else dn_mid
            signal = cont_ok and el_vel >= EE_VEL_MIN and EE_ENTRY_LO <= el_bid <= EE_ENTRY_HI
            if signal:
                win = (el_side == 'UP' and resolution == 1) or \
                      (el_side == 'DOWN' and resolution == 0)
                results_ee_ng.append({
                    'market_id': mkt_id, 'entry_secs': secs,
                    'entry_price': round(el_bid, 4), 'el_vel': el_vel,
                    'n_s180': len(s180), 'el_side': el_side,
                    'resolution': resolution, 'win': win,
                })
                entered = True

df_ee_ng = pd.DataFrame(results_ee_ng)

# ── Backtest AR simples ───────────────────────────────────────────────────────
# Proxy do AR: entra quando um lado atinge > threshold com secs <= 60.
# O setup real é mais complexo (distance_bps, variant, etc.) mas este
# captura a lógica central.
print("Rodando backtest AR...")
results_ar = []
AR_THRESHOLD = 0.88  # proxy de "almost resolved"
AR_MAX_SECS  = 60

for mkt_id, grp in p5_res.groupby('market_id'):
    resolution = res_map[mkt_id]
    rows = grp.sort_values('timestamp')[['secs','up_price','down_price']].values
    entered = False
    for secs, up_mid, dn_mid in rows:
        secs = int(secs)
        if secs > AR_MAX_SECS or entered:
            continue
        for side, price in [('UP', up_mid), ('DOWN', dn_mid)]:
            if price >= AR_THRESHOLD:
                win = (side == 'UP' and resolution == 1) or \
                      (side == 'DOWN' and resolution == 0)
                results_ar.append({
                    'market_id': mkt_id, 'entry_secs': secs,
                    'entry_price': round(price, 4),
                    'side': side, 'resolution': resolution, 'win': win,
                })
                entered = True
                break

df_ar = pd.DataFrame(results_ar)

# ── Resultados ────────────────────────────────────────────────────────────────
def show(df, label):
    if df.empty:
        print(f"\n{label}: 0 entradas encontradas.")
        return
    n   = len(df)
    wr  = df['win'].mean()
    ep  = df['entry_price'].mean()
    print(f"\n{'─'*55}")
    print(f"  {label}")
    print(f"{'─'*55}")
    print(f"  Entradas :  {n:>6,}")
    print(f"  WR       :  {wr:.1%}")
    print(f"  EP médio :  {ep:.3f}")
    print(f"  Mercados :  {df['market_id'].nunique():>6,}")

    if 'el_vel' in df.columns:
        print(f"\n  WR por faixa de el_vel:")
        df['vel_bin'] = pd.cut(df['el_vel'],
            bins=[0.17, 0.25, 0.35, 0.50, 1.0],
            labels=['0.17-0.25','0.25-0.35','0.35-0.50','>0.50'])
        for vb, g in df.groupby('vel_bin', observed=True):
            print(f"    vel {vb}: n={len(g):>4}  WR={g['win'].mean():.1%}  ep={g['entry_price'].mean():.3f}")

        print(f"\n  WR por faixa de entry_price:")
        df['ep_bin'] = pd.cut(df['entry_price'],
            bins=[0.81, 0.82, 0.83, 0.84, 0.85, 0.86],
            labels=['0.82','0.83','0.84','0.85','0.85+'])
        for eb, g in df.groupby('ep_bin', observed=True):
            print(f"    ep {eb}: n={len(g):>4}  WR={g['win'].mean():.1%}")

        print(f"\n  WR por faixa de entry_secs:")
        df['secs_bin'] = pd.cut(df['entry_secs'],
            bins=[29, 60, 90, 120, 180],
            labels=['30-60','61-90','91-120','121-180'])
        for sb, g in df.groupby('secs_bin', observed=True):
            print(f"    secs {sb}: n={len(g):>4}  WR={g['win'].mean():.1%}")

    if 'entry_price' in df.columns and 'side' in df.columns:
        print(f"\n  WR por faixa de entry_price (AR):")
        df['ep_bin'] = pd.cut(df['entry_price'],
            bins=[0.87, 0.90, 0.93, 0.96, 1.01],
            labels=['0.88-0.90','0.90-0.93','0.93-0.96','>0.96'])
        for eb, g in df.groupby('ep_bin', observed=True):
            print(f"    ep {eb}: n={len(g):>4}  WR={g['win'].mean():.1%}")

print(f"\n{'='*55}")
print(f"  BACKTEST — BrockMisner BTC 5min")
print(f"  Periodo: fev-abr 2026  |  Mid-price (bid ≈ mid-0.01)")
print(f"{'='*55}")
print(f"  Mercados totais BTC 5min : {len(mkt):>6,}")
print(f"  Com resolução conhecida  : {len(resolved_ids):>6,}")

show(df_ee,    "EE  com gate n_s180>=2 (calibrado)")
show(df_ee_ng, "EE  sem gate n_s180    (controle)")
show(df_ar,    "AR  proxy threshold>=0.88  secs<=60")

# Salvar resultados
df_ee.to_csv('_result_ee.csv', index=False)
df_ar.to_csv('_result_ar.csv', index=False)
print(f"\nResultados salvos em _result_ee.csv e _result_ar.csv")
