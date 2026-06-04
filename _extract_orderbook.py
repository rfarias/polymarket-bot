#!/usr/bin/env python3
"""Extrai do orderbook BTC 5min apenas os mercados com resolucao conhecida."""
import warnings; warnings.filterwarnings('ignore')
import sys, time
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

import pandas as pd
import pyarrow.parquet as pq
import pyarrow as pa
import pyarrow.compute as pc

# Mercados com resolucao direta (1=UP, 0=DOWN)
mkt = pd.read_parquet('_btc5_markets.parquet')
resolved = mkt[mkt['resolution'].isin([0, 1])].copy()
resolved['market_id'] = resolved['market_id'].astype(str)
res_ids = set(resolved['market_id'].tolist())
print(f'Mercados com resolucao direta: {len(res_ids)}')

# Adicionar mercados com resolucao inferida pelo preco final (prices table)
# carrega ultima linha de cada mercado da tabela prices
try:
    p5 = pd.read_parquet('_btc5_prices.parquet', columns=['market_id','timestamp','up_price'])
    p5['market_id'] = p5['market_id'].astype(str)
    last_p = p5.sort_values('timestamp').groupby('market_id').last().reset_index()
    infer_up   = set(last_p[last_p['up_price'] > 0.92]['market_id'])
    infer_down = set(last_p[last_p['up_price'] < 0.08]['market_id'])
    infer_ids  = (infer_up | infer_down) - res_ids
    print(f'Mercados com resolucao inferida (preco final): {len(infer_ids)}')

    # Monta res_map completo
    res_map = dict(zip(resolved['market_id'], resolved['resolution'].astype(int)))
    for mid in infer_up:   res_map[mid] = 1
    for mid in infer_down: res_map[mid] = 0
    all_target = set(res_map.keys())
except Exception as e:
    print(f'  nao conseguiu inferir: {e}')
    res_map = dict(zip(resolved['market_id'], resolved['resolution'].astype(int)))
    all_target = res_ids

# Usa apenas os 616 mercados com resolucao DIRETA (mais confiavel + menor volume)
all_target = set(resolved['market_id'].tolist())
print(f'Usando apenas mercados com resolucao direta: {len(all_target)}')
target_pa = pa.array(sorted(all_target), type=pa.string())

# Atualiza res_map para so estes mercados
res_map = dict(zip(resolved['market_id'], resolved['resolution'].astype(int)))
pd.DataFrame(list(res_map.items()), columns=['market_id','resolution'])\
  .to_parquet('_btc5_res_map.parquet', index=False)

# Leitura row-group por row-group, salvando em disco a cada FLUSH_EVERY grupos
print('Lendo orderbook por row group com flush periodico...')
t0 = time.time()
pf = pq.ParquetFile('_btc5_orderbook.parquet')
n_groups = pf.metadata.num_row_groups
print(f'Row groups: {n_groups:,}  |  Total rows: {pf.metadata.num_rows:,}')

COLS        = ['ts_ms', 'market_id', 'outcome', 'best_bid']
FLUSH_EVERY = 300   # salva em disco a cada N grupos com matches
FLUSH_ROWS  = 5_000_000  # ou quando acumular >5M linhas

chunks        = []
total_rows    = 0
flushed_rows  = 0
found_markets = set()
part_idx      = 0

import os
for f in ['_ob_part0.parquet','_ob_part1.parquet','_ob_part2.parquet',
          '_ob_part3.parquet','_ob_part4.parquet']:
    if os.path.exists(f): os.remove(f)

def flush(chunks, part_idx):
    if not chunks:
        return part_idx
    df = pd.concat(chunks, ignore_index=True)
    df = df.drop_duplicates(subset=['ts_ms', 'market_id', 'outcome'])
    df.to_parquet(f'_ob_part{part_idx}.parquet', index=False)
    print(f'    >> flush part{part_idx}: {len(df):,} linhas (dedup)')
    return part_idx + 1

match_groups = 0
for i in range(n_groups):
    mid_col = pf.read_row_group(i, columns=['market_id']).column('market_id')
    if not pc.any(pc.is_in(mid_col, value_set=target_pa)).as_py():
        continue

    rg = pf.read_row_group(i, columns=COLS).to_pandas()
    rg = rg[rg['market_id'].isin(all_target)]
    if rg.empty:
        continue

    chunks.append(rg)
    total_rows += len(rg)
    found_markets.update(rg['market_id'].unique())
    match_groups += 1

    if match_groups % FLUSH_EVERY == 0 or total_rows - flushed_rows >= FLUSH_ROWS:
        part_idx = flush(chunks, part_idx)
        flushed_rows += total_rows - flushed_rows
        chunks = []

    if (i + 1) % 1000 == 0:
        elapsed = time.time() - t0
        print(f'  {i+1:>5}/{n_groups}  {(i+1)/n_groups:.0%}  '
              f'rows={total_rows:,}  mkts={len(found_markets)}  t={elapsed:.0f}s')

# Flush final
part_idx = flush(chunks, part_idx)

# Combina partes
parts = [f'_ob_part{j}.parquet' for j in range(part_idx) if os.path.exists(f'_ob_part{j}.parquet')]
ob = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
ob = ob.drop_duplicates(subset=['ts_ms', 'market_id', 'outcome'])

elapsed = time.time() - t0
print(f'Concluido em {elapsed:.0f}s  |  linhas: {len(ob):,}  |  mercados: {ob["market_id"].nunique():,}')
print(f'outcome unicos: {ob["outcome"].unique()}')
print(f'best_bid range: {ob["best_bid"].min():.3f} a {ob["best_bid"].max():.3f}')

ob.to_parquet('_btc5_ob_filtered.parquet', index=False)
for p in parts:
    os.remove(p)
print(f'Salvo: _btc5_ob_filtered.parquet  ({os.path.getsize("_btc5_ob_filtered.parquet")/1e6:.0f} MB no disco)')
