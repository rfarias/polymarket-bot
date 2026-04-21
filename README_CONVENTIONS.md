# README_CONVENTIONS

Este documento define as convenções práticas já usadas no repositório e que devem ser preservadas para manter continuidade.

## 1. Naming de arquivos

### 1.1 Runners

Use `run_*` para ponto de entrada operacional.

Exemplos:

- `run_guarded_bot.py`
- `run_live_next1_scalp_real_v1.py`
- `run_multi_setup_bot.py`

Regra:

- se o arquivo é o comando que um operador roda, ele deve ser `run_*`

### 1.2 Diagnostics

Use `diagnostics_*` para testes de hipótese, smoke tests, preflight, paper trading e regressão.

Exemplos:

- `diagnostics_live_guard_preflight.py`
- `diagnostics_next1_scalp_paper_v1.py`
- `diagnostics_regression_suite_v1.py`

Regra:

- `diagnostics_*` não deve virar dependência implícita da execução real

### 1.3 Módulos de mercado

Use `market/*` para lógica de domínio:

- sinais
- execução
- broker
- reconciliação
- políticas
- workflow real

### 1.4 Sufixos de contexto

Use nomes que deixem explícito o contexto:

- `*_real_*`: fluxo com posting real
- `*_paper_*`: simulação/paper trading
- `*_projection_*`: estimativa ou cenário
- `*_signal_*`: geração de sinal
- `*_workflow_*`: regras de transição de execução

## 2. Versionamento

O repositório usa sufixos `v1`, `v2`, `v3`, etc.

Regra operacional:

- não apague uma versão anterior sem necessidade explícita
- não assuma que a maior versão substitui toda anterior
- documente qual versão está canônica no README quando introduzir nova versão

Ao criar uma nova versão:

- preserve a antiga se ela ainda for referência operacional
- atualize a documentação indicando a nova fonte de verdade
- não troque silenciosamente o runner sem atualizar os READMEs

## 3. Padrão de rollout real

Todo rollout real deve manter estes elementos:

- preflight explícito
- validação de `.env`
- guard rails de `shadow_only` e `real_posts_enabled`
- startup guard antes de iniciar
- logs persistidos em `logs/`
- algum mecanismo de recovery/reconciliação

Se um novo runner real não tiver esses itens, ele está abaixo do padrão atual do projeto.

## 4. Separação de responsabilidades

### 4.1 Sinal

Responsável por:

- avaliar mercado
- calcular edge
- retornar decisão estruturada

Não deve:

- postar ordens
- cancelar ordens
- consultar estado de broker como parte do core decisório

### 4.2 Runner

Responsável por:

- bootstrap
- preflight
- loop
- logging
- coordenação entre sinal e broker

### 4.3 Broker

Responsável por:

- autenticação
- healthcheck
- posting/cancelamento
- fetch de ordens
- allowance e saldo

## 5. Persistência

Padrão atual:

- arquivos de sessão em `logs/<nome_do_runner>_<timestamp>/`
- estado resumido em arquivo JSON dedicado quando o runner precisa recovery

Exemplo:

- `logs/next1_scalp_real_20260420_.../next1_scalp_real.jsonl`
- `logs/next1_scalp_real_state.json`

Regra:

- se o runner pode deixar posição ou ordem pendente entre reinícios, ele precisa persistir estado explícito

## 6. Logs

O projeto favorece logs legíveis em texto e JSONL de eventos.

Prática recomendada:

- `print(...)` para telemetria operacional rápida
- JSONL para trilha posterior e debugging

Ao adicionar um novo runner:

- mantenha prints com tags estáveis como `[BOOT]`, `[RESULT]`, `[GUARD]`, `[RUN]`
- grave snapshots e eventos críticos em JSONL

## 7. Critério para arquivo canônico

Considere um módulo "canônico" quando ele cumpre todos estes pontos:

- é referenciado por um `run_*`
- tem preflight ou guard rails equivalentes
- usa o broker e a persistência de forma operacional
- aparece na documentação principal

Se faltar isso, trate como experimento, suporte ou material de diagnóstico.

## 8. Como adicionar um novo setup

Fluxo recomendado:

1. Criar primeiro o `*_signal_vN.py`.
2. Criar diagnóstico paper ou projection.
3. Criar runner real dedicado.
4. Adicionar preflight forte.
5. Adicionar persistência de sessão e recovery se aplicável.
6. Atualizar `.env.example`.
7. Atualizar `README.md` e o README específico do setup.

## 9. Como fazer handoff limpo

Antes de considerar um setup "entregável" para outro programador:

- documente fonte de verdade
- documente env vars
- documente máquina de estado, se houver
- documente recovery
- documente o que é experimental e o que é ativo

Esse é o padrão que o projeto deve manter daqui para frente.
