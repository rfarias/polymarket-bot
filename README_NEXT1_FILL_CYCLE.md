# README_NEXT1_FILL_CYCLE

Este documento descreve o setup hedgeado de `next_1` usado pelo runner guardado principal.

## 1. Fonte de verdade

Arquivos canonicos:

- [run_guarded_bot.py](/C:/Users/Letícia/Documents/polymarket-bot/run_guarded_bot.py)
- [market/live_real_fill_cycle_v2.py](/C:/Users/Letícia/Documents/polymarket-bot/market/live_real_fill_cycle_v2.py)
- [market/setup1_broker_executor_v4.py](/C:/Users/Letícia/Documents/polymarket-bot/market/setup1_broker_executor_v4.py)
- [market/real_execution_workflow_v2.py](/C:/Users/Letícia/Documents/polymarket-bot/market/real_execution_workflow_v2.py)
- [market/broker_startup_guard_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/broker_startup_guard_v1.py)

## 2. Ideia do setup

Esse setup nao e direcional. Ele tenta montar uma entrada hedgeada no `next_1` comprando os dois lados quando o book permite arbitragem suficientemente barata.

Objetivo operacional:

- abrir plano de duas pernas em `next_1`
- acompanhar fills pelo broker
- lidar com casos de fill desbalanceado
- fechar o risco por deadline, single-leg trailing ou force-close

## 3. Gatilho de entrada

O loop usa snapshots publicos do `next_1` e calcula:

- metricas de display
- metricas executaveis
- classificacao de sinal
- continuation filter

A entrada so e considerada quando:

- o slot e `next_1`
- `sum_asks <= ARBITRAGE_SUM_ASKS_MAX`
- o continuation filter nao bloqueia a entrada
- o sinal estabiliza em `armed`
- ainda existe tempo suficiente antes do deadline

## 4. Executor e plano

O setup usa `Setup1BrokerExecutorV4`.

Esse executor:

- cria um plano de duas pernas
- registra `up_entry` e `down_entry`
- posta ou simula ordens dependendo de `shadow_only`
- guarda `active_plan_id` por slot

O estado do plano nao fica apenas no broker. Ele tambem existe no `order_manager` e na persistencia local.

## 5. Estados praticos do fluxo

Embora o executor use estados internos do `Setup1OrderManagerV2`, a logica principal do setup pode ser lida assim:

- sem plano ativo
  - espera um `armed` valido em `next_1`

- plano criado e working
  - acompanha as duas pernas
  - sincroniza `size_matched` com ordens abertas do broker

- hedge balanceado
  - quando as duas pernas enchem de forma adequada
  - hoje a regra padrao e segurar ate a resolucao, sem postar exit imediato

- single-leg
  - uma perna encheu e a outra nao
  - o workflow pode cancelar a perna remanescente e acompanhar trailing
  - se o trailing acionar, posta saida real da perna preenchida

- deadline / abort / force-close
  - cancela ordens restantes
  - se houver risco residual, tenta saidas de force-close
  - limpa o plano quando vira terminal

## 6. O que o workflow real faz

`real_execution_workflow_v2.py` concentra as regras de execucao real:

- `maybe_take_single_leg_profit_real_v2`
  - trailing em caso de fill unilateral

- `maybe_post_balanced_exit_orders_v2`
  - no caso hedgeado, hoje apenas registra que o hedge ficou balanceado e segura

- `handle_deadline_real_v2`
  - aplica deadline do setup

- `maybe_post_force_close_exits_v2`
  - tenta postar saidas quando o plano precisa ser zerado

- `cleanup_terminal_plan_v2`
  - limpa plano terminal do executor

## 7. Persistencia e recovery

O fluxo usa persistencia do executor:

- restore no bootstrap
- flush apos avaliar slot
- flush apos sync com broker

Se existir estado local restaurado sem correspondencia com ordens abertas, o runner limpa a persistencia stale antes de seguir.

## 8. Guard rails

O runner exige:

- `POLY_GUARDED_ENABLED=true`
- `POLY_GUARDED_SHADOW_ONLY=false`
- `POLY_GUARDED_REAL_POSTS_ENABLED=true`
- `POLY_GUARDED_ALLOW_NEXT_2=false`
- `POLY_GUARDED_MAX_ACTIVE_PLANS=1`
- `POLY_GUARDED_MIN_SHARES=5`
- `POLY_GUARDED_REQUIRE_SIGNAL=armed`

Tambem exige:

- `broker healthcheck` ok
- `startup guard` ok

## 9. Caminho paper -> real

Para evoluir esse setup com seguranca:

1. Validar diagnosticos e regressao.
2. Rodar o runner em ambiente de broker autenticado com preflight.
3. Operar com sizing minimo e guard rails atuais.
4. Revisar logs de reconcile, deadline e cleanup.
5. So depois aumentar cobertura ou paralelismo.

## 10. Papel futuro no modo simultaneo

Esse setup ja nasce mais compativel com execucao simultanea porque:

- ja tem executor dedicado
- ja usa startup guard
- ja usa reconcile continuo
- ja persiste estado

Para rodar junto com outros setups reais, ele deve continuar sendo o bloco mais estrito de controle de risco do `next_1`.
