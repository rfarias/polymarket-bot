# README_ARCHITECTURE

Este documento resume a arquitetura atual do projeto `polymarket-bot` com foco no que estA ativo hoje e no que deve ser tratado como base para continuidade.

## 1. Objetivo do projeto

O repositório concentra runners e pesquisas para operar mercados BTC 5m da Polymarket em diferentes estilos:

- `next_1` fill-cycle hedgeado
- `next1 scalp` direcional de curta duração
- `current scalp`
- `current almost resolved`

Nem todo arquivo do repositório é "canônico". Há muito material de diagnóstico, paper trading e versões anteriores.

## 2. Camadas principais

### 2.1 Runners

São os pontos de entrada usados para operação ou preflight:

- [run_guarded_bot.py](/C:/Users/Letícia/Documents/polymarket-bot/run_guarded_bot.py): runner real do fill-cycle em `next_1`
- [run_live_next1_scalp_real_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/run_live_next1_scalp_real_v1.py): runner real dedicado ao setup `next1 scalp`
- [run_live_current_scalp_real_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/run_live_current_scalp_real_v1.py): runner real dedicado ao setup `current scalp`
- [run_multi_setup_bot.py](/C:/Users/Letícia/Documents/polymarket-bot/run_multi_setup_bot.py): runner combinado para `next_1` + setups do `current`
- [run_scalp_reversal_bot.py](/C:/Users/Letícia/Documents/polymarket-bot/run_scalp_reversal_bot.py): runner de scalp reversal

### 2.2 Sinais e pesquisa

Esses módulos transformam snapshot de mercado + contexto externo em sinais:

- [market/next1_scalp_signal_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/next1_scalp_signal_v1.py): fonte de verdade do sinal do `next1 scalp`
- [market/current_scalp_signal_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/current_scalp_signal_v1.py): sinal do `current scalp`
- [market/live_current_scalp_real_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/live_current_scalp_real_v1.py): execução real dedicada do `current scalp`
- [market/current_almost_resolved_signal_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/current_almost_resolved_signal_v1.py): sinal do fallback quase resolvido

### 2.3 Broker e integração real

Essa camada encapsula o acesso autenticado ao CLOB:

- [market/polymarket_broker_v3.py](/C:/Users/Letícia/Documents/polymarket-bot/market/polymarket_broker_v3.py): wrapper principal do broker real
- [market/broker_env.py](/C:/Users/Letícia/Documents/polymarket-bot/market/broker_env.py): validação do ambiente de credenciais
- [market/broker_types.py](/C:/Users/Letícia/Documents/polymarket-bot/market/broker_types.py): tipos normalizados de ordem, request e health

### 2.4 Guard rails e reconciliação

Essa camada existe para impedir que o bot comece "por cima" de ordens abertas ou estado inconsistente:

- [market/live_guarded_config.py](/C:/Users/Letícia/Documents/polymarket-bot/market/live_guarded_config.py): flags operacionais globais
- [market/broker_startup_guard_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/broker_startup_guard_v1.py): bloqueia início com ordens externas ou desconhecidas
- [market/real_execution_workflow_v2.py](/C:/Users/Letícia/Documents/polymarket-bot/market/real_execution_workflow_v2.py): regras de sync, trailing, exits e force-close do fluxo hedgeado

### 2.5 Snapshot público e descoberta

Esses módulos alimentam os sinais e loops:

- `rest_5m_shadow_public_v5.py`: monta o bundle de slots, baixa snapshots e produz métricas executáveis
- `book_5m.py`: baixa book de token para casos de monitoramento da posição ativa
- `slug_discovery.py`: descoberta/detalhe de mercado por slug

## 3. Fluxos ativos

### 3.1 Fill-cycle hedgeado de `next_1`

Fluxo canônico:

1. [run_guarded_bot.py](/C:/Users/Letícia/Documents/polymarket-bot/run_guarded_bot.py) valida `.env`
2. [market/live_real_fill_cycle_v2.py](/C:/Users/Letícia/Documents/polymarket-bot/market/live_real_fill_cycle_v2.py) roda o loop principal
3. [market/setup1_broker_executor_v4.py](/C:/Users/Letícia/Documents/polymarket-bot/market/setup1_broker_executor_v4.py) cria e acompanha planos
4. [market/real_execution_workflow_v2.py](/C:/Users/Letícia/Documents/polymarket-bot/market/real_execution_workflow_v2.py) gerencia sync, deadline, single-leg e exits

Esse fluxo é o rollout "guardado" mais tradicional do projeto.

### 3.2 Setup direcional `next1 scalp`

Fluxo canônico:

1. [run_live_next1_scalp_real_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/run_live_next1_scalp_real_v1.py) faz preflight
2. [market/live_next1_scalp_real_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/live_next1_scalp_real_v1.py) roda o loop real
3. [market/next1_scalp_signal_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/next1_scalp_signal_v1.py) produz o sinal direcional
4. O runner salva estado próprio em `logs/next1_scalp_real_state.json`

Esse fluxo é independente do `Setup1BrokerExecutorV4`. Ele tem máquina de estado própria.

### 3.3 Multi-setup

[market/live_multi_setup_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/live_multi_setup_v1.py) combina:

- fill-cycle de `next_1`
- `current scalp`
- `current almost resolved`

Esse runner é útil como integração entre estratégias, mas ele adiciona mais superfície operacional do que os runners dedicados.

## 4. Arquivos canônicos vs experimentais

### 4.1 Tratar como canônicos

- `run_guarded_bot.py`
- `run_live_next1_scalp_real_v1.py`
- `market/live_real_fill_cycle_v2.py`
- `market/live_next1_scalp_real_v1.py`
- `market/next1_scalp_signal_v1.py`
- `market/current_scalp_signal_v1.py`
- `market/current_almost_resolved_signal_v1.py`
- `market/polymarket_broker_v3.py`
- `market/broker_env.py`
- `market/live_guarded_config.py`
- `market/broker_startup_guard_v1.py`
- `market/real_execution_workflow_v2.py`

### 4.2 Tratar como suporte ou diagnóstico

- `diagnostics_*`
- `run_next1_scalp_projection_paper_v1.py`
- `diagnostics_next1_scalp_paper_v1.py`
- `diagnostics_next1_scalp_projection_paper_v1.py`

Esses arquivos ajudam a validar hipótese, edge e operação em paper, mas não são a fonte de verdade da execução real.

### 4.3 Tratar com cautela

Arquivos com sufixos `v1`, `v2`, `v3`, `v4`, `v5` precisam ser avaliados pelo papel atual, não pela versão numérica. Neste repositório, a maior versão nem sempre significa "a única ativa", e várias versões antigas continuam presentes como histórico operacional.

## 5. Estado e persistência

Hoje existem dois padrões principais de estado:

- Estado interno do executor hedgeado
  - mantido pelo `Setup1OrderManagerV2`
  - reconciliado com ordens abertas do broker
  - persistido por módulos de `executor_state_store_v1`

- Estado dedicado do `next1 scalp`
  - serializado em `logs/next1_scalp_real_state.json`
  - restaurado no bootstrap do runner
  - usado para impedir restart com posição ou ordens em aberto não reconciliadas

## 6. Regra prática para continuidade

Se outro programador for continuar o projeto nos mesmos padrões:

- use runners dedicados como ponto de entrada
- preserve preflight forte antes de qualquer rollout real
- preserve startup guard antes de iniciar loops reais
- preserve persistência e reconciliação antes de expandir volume ou paralelismo
- trate `diagnostics_*` como suporte, não como API estável

## 6.1 Fluxo De Agente Esperado

O projeto deve ser conduzido em ciclos completos sempre que a acao for segura:

- rodar paper, replay ou diagnostico
- medir funil, PnL, fills, slippage e risco de saida
- identificar o gargalo principal
- implementar ajuste pequeno
- compilar/testar
- documentar
- commitar
- deixar handoff ou comando de continuidade pronto

O agente/programador pode prosseguir sem pedir confirmacao para analise de logs, paper trading sem ordens reais, replay historico, melhorias de diagnostico, documentacao e commits de mudancas testadas.

O agente/programador deve pedir confirmacao antes de postar ordens reais, aumentar tamanho de mao, remover travas de risco, alterar credenciais, ligar autonomia de execucao ou apagar logs/estado operacional.

## 7. Próximo lugar para olhar no código

Para continuar `next1 scalp`:

- [README_NEXT1_SCALP.md](/C:/Users/Letícia/Documents/polymarket-bot/README_NEXT1_SCALP.md)
- [README_NEXT1_FILL_CYCLE.md](/C:/Users/Letícia/Documents/polymarket-bot/README_NEXT1_FILL_CYCLE.md)
- [README_CURRENT_SETUPS.md](/C:/Users/Letícia/Documents/polymarket-bot/README_CURRENT_SETUPS.md)
- [README_SCALP_REVERSAL.md](/C:/Users/Letícia/Documents/polymarket-bot/README_SCALP_REVERSAL.md)
- [README_ROADMAP_MULTI_REAL.md](/C:/Users/Letícia/Documents/polymarket-bot/README_ROADMAP_MULTI_REAL.md)

Para convenções de naming, rollout e criação de novos módulos:

- [README_CONVENTIONS.md](/C:/Users/Letícia/Documents/polymarket-bot/README_CONVENTIONS.md)
