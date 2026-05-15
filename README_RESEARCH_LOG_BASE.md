# Research Log Base

Este repositorio agora tem uma base unica para estudos estatisticos dos runners.

## Objetivo

Os runners continuam gravando seus proprios arquivos em `logs/**/*.jsonl`. Para analise, o script `build_research_log_base_v1.py` varre todos esses arquivos e gera uma base normalizada com campos comuns, permitindo comparar setups diferentes sem depender de um log isolado.

## Gerar ou Atualizar

Depois de deixar coletores rodando, execute:

```powershell
python build_research_log_base_v1.py
```

Saidas geradas:

```text
logs/research_base/research_events_all_v1.jsonl
logs/research_base/research_events_all_v1_summary.json
```

O script ignora `logs/research_base` para nao reprocessar a propria base gerada. Portanto, pode ser executado quantas vezes forem necessarias.

## Campos Normalizados

Principais campos disponiveis na base:

```text
source_file
source_line
runner
type
ts
slug
secs_to_end
setup
setup_variant
allow
reason
side
leader_price
counter_price
entry_price
exit_price
exit_reason
pnl_ticks
distance_from_open_bps
distance_to_price_to_beat_bps
distance_to_price_to_beat_usd
buffer_bps
buffer_usd
spot_delta_5s_bps
spot_delta_15s_bps
spot_delta_30s_bps
market_range_30s
green_hold_ready
gray_target_stop_ready
```

## Uso nas Proximas Analises

Antes de rodar novas estatisticas, primeiro atualize a base:

```powershell
python build_research_log_base_v1.py
```

Depois use `logs/research_base/research_events_all_v1.jsonl` como fonte principal. Isso permite estudar:

- quase resolvido com stop;
- entrada passiva, agressiva limitada e hibrida;
- reversao contra quase resolvido;
- tendencia com stop movel;
- padroes de abertura da proxima janela apos extreme resolvido;
- comportamento por tempo restante, distancia do price to beat, volatilidade e direcao do candle anterior.

## Situacao Atual

Na primeira consolidacao feita nesta maquina, a base agregou:

```text
614 arquivos JSONL
94.289 eventos normalizados
420 janelas distintas de current_almost_resolved
528 janelas distintas de next1
350 eventos de saida no quase resolvido
175 fills no quase resolvido
```

Esses numeros devem aumentar sempre que os runners seguirem coletando dados e o script for executado novamente.
