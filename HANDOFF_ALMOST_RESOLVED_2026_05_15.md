# Handoff Almost Resolved - 2026-05-15

## Estado Atual

Branch local:

```text
main
```

Objetivo atual:

```text
validar o setup current almost-resolved completo em paper realista antes de rodar ordens reais
```

O foco deixou de ser apenas aumentar fills. O criterio principal agora e consistencia:

```text
mais oportunidades boas
perda ruim proporcional ao ganho de uma ou poucas maos
nenhuma entrada quando a saida depende de book fino
nenhuma perda isolada capaz de devolver o lucro de um dia ou semana
```

## O Que Foi Implementado

Arquivo principal:

```text
diagnostics_current_almost_resolved_paper_v1.py
```

Mudancas principais:

```text
gray-zone opcional via --enable-gray-zone
entrada agressiva restrita aos mesmos cenarios seguros do real
split 50/50 opcional para passive_extreme_liquidity_capture ficando extreme resolved
estatisticas por entry_order_style, setup_variant e source
funil de execucao em execution_funnel
metrica signal.planned_exit_risk para medir risco realista de saida
```

A nova metrica `signal.planned_exit_risk` aparece nos sinais liberados e mede:

```text
best_bid
observed_bid_depth
qty_at_or_above_stop
qty_at_best_three
vwap_exit_for_qty
worst_price_for_qty
enough_depth_for_qty
exit_depth_covers_stop
theoretical_stop_loss_ticks
pessimistic_exit_loss_ticks
worst_level_loss_ticks
```

## Replay Historico

Novo script:

```text
analyze_full_setup_research_base_v1.py
```

Ele reprocessa o log consolidado:

```text
logs/research_base/research_events_all_v1.jsonl
```

Limite importante: esse replay nao tem o book completo original, entao usa proxies e serve para triagem, nao para validar fills reais.

Resultados relevantes ja observados:

```text
modo agressivo antigo/otimista: muitos trades e PnL positivo, mas irrealista
modo chase realista: muitos sinais, praticamente nenhum fill confirmado no historico consolidado
split 50/50 historico: amostra pequena demais para conclusao
```

Conclusao: precisamos medir fill conversion em paper live e depois em real supervisionado pequeno.

## Papers Rodando Nesta Maquina

No momento desta documentacao, os processos ativos eram:

```text
paper principal: PID 6468
paper split 50/50: PID 24604
runner da extensao: PID 25112
```

Logs novos com a metrica de risco de saida:

```text
logs/current_almost_resolved_full_setup_paper_v1/full_setup_live_exit_risk.jsonl
logs/current_almost_resolved_full_setup_paper_v1/full_setup_split_50_50_exit_risk.jsonl
```

Logs antigos antes de reiniciar com `planned_exit_risk`:

```text
logs/current_almost_resolved_full_setup_paper_v1/full_setup_live.jsonl
logs/current_almost_resolved_full_setup_paper_v1/full_setup_split_50_50_live.jsonl
```

Parcial antiga observada:

```text
paper principal:
  signal_allowed: 24
  order_candidate: 24
  passive_limit_placed: 9
  passive_limit_touch: 2
  aggressive_limit_skip: 15
  trade_opened_from_fill: 0

paper split:
  signal_allowed: 19
  order_candidate: 19
  passive_limit_placed: 6
  aggressive_limit_skip: 13
  trade_opened_from_fill: 0
```

Interpretacao: o paper ficou conservador. Apareceram oportunidades, mas ainda sem fill confirmado. O funil morreu em passiva nao preenchida e agressiva bloqueada pelas novas travas.

## Comandos Para Continuar Em Casa

Atualizar repositorio:

```powershell
git pull
python -m py_compile diagnostics_current_almost_resolved_paper_v1.py analyze_full_setup_research_base_v1.py
```

Rodar paper principal com risco de saida:

```powershell
python diagnostics_current_almost_resolved_paper_v1.py --seconds 21600 --poll-secs 1 --order-qty 6 --hybrid-passive-to-aggressive --hybrid-aggressive-after-secs 2 --hybrid-aggressive-max-price 0.99 --passive-fill-touch-polls 2 --hold-winner-to-resolution --resolution-settle-secs 1 --enable-gray-zone --log-file logs\current_almost_resolved_full_setup_paper_v1\full_setup_live_exit_risk.jsonl
```

Rodar split 50/50 em paper separado:

```powershell
python diagnostics_current_almost_resolved_paper_v1.py --seconds 21600 --poll-secs 1 --order-qty 100 --hybrid-passive-to-aggressive --hybrid-aggressive-after-secs 2 --hybrid-aggressive-max-price 0.99 --passive-fill-touch-polls 2 --hold-winner-to-resolution --resolution-settle-secs 1 --enable-gray-zone --split-extreme-entry --split-aggressive-frac 0.5 --maker-rebate-bps 0 --log-file logs\current_almost_resolved_full_setup_paper_v1\full_setup_split_50_50_exit_risk.jsonl
```

Runner da extensao:

```powershell
python run_manual_signal_server_v1.py --qty 6
```

Endpoint esperado:

```text
http://127.0.0.1:8765/state
```

## Proximo Passo Recomendado

Nao adicionar novas features agora. Primeiro analisar os logs novos:

```text
execution_funnel
stats.by_entry_order_style
signal.planned_exit_risk
trade_opened_from_fill
pessimistic_exit_loss_ticks
exit_depth_covers_stop
```

So considerar runner real com `qty 6` depois que o paper mostrar:

```text
fills suficientes para medir
perda pessimista controlada
nenhum caso de saida dependente de book fino
agressiva trazendo ganho incremental ou sendo removida
```

## Roadmap Registrado

Tambem ficou documentado:

```text
futuro runner em Go somente para execucao real, nao para substituir pesquisa Python
futuro agente parcialmente autonomo apenas como consultor inicialmente
analise grafica como feature de contexto no paper antes de virar decisao real
```
