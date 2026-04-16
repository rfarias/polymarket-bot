# README_TESTS

Este arquivo organiza a trilha de testes do projeto `polymarket-bot`, separando o que pode ser executado **sem wallet / sem broker autenticado** do que depende do ambiente real da Polymarket.

## 1. Testes offline (não exigem wallet)

Esses testes podem ser rodados em qualquer máquina com Python e o repositório atualizado.

### 1.1 Rodar a suíte completa

```bash
python diagnostics_regression_suite_v1.py
```

Essa suíte executa:
- `diagnostics_broker_reconcile_v1.py`
- `diagnostics_broker_status_sync_v1.py`
- `diagnostics_broker_status_sync_v2.py`
- `diagnostics_balanced_hedge_hold_v1.py`

Saída esperada no final:
- `TOTAL=4 PASS=4 FAIL=0`
- `[RESULT] Regression suite finished successfully`

### 1.2 Teste isolado: reconciliação de ordens

```bash
python diagnostics_broker_reconcile_v1.py
```

Valida:
- ordem `tracked`
- ordem `external`
- ordem com `unknown_client_key`

### 1.3 Teste isolado: status sync básico

```bash
python diagnostics_broker_status_sync_v1.py
```

Valida:
- `fill_delta`
- partial fill
- full fill
- cancelamento vindo do broker

### 1.4 Teste isolado: status sync com terminalização

```bash
python diagnostics_broker_status_sync_v2.py
```

Valida:
- cenário desbalanceado
- `FORCE_CLOSE`
- `PLAN_END next_1: force_closed`
- `active_plan_id = None`

### 1.5 Teste isolado: hedge balanceado sem saída no book

```bash
python diagnostics_balanced_hedge_hold_v1.py
```

Valida:
- plano em estado `hedged`
- nenhuma criação de `up_exit/down_exit`
- nenhuma postagem de saída no broker para hedge balanceado

## 2. Testes com broker autenticado (PC de casa)

Esses testes exigem `.env` configurado com credenciais válidas do broker real.

### 2.1 Pré-requisitos do `.env`

Exemplo mínimo para runners protegidos:

```env
POLY_GUARDED_ENABLED=true
POLY_GUARDED_SHADOW_ONLY=false
POLY_GUARDED_REAL_POSTS_ENABLED=true
POLY_GUARDED_MAX_ACTIVE_PLANS=1
POLY_GUARDED_ALLOW_NEXT_2=false
POLY_GUARDED_MIN_SHARES=5
POLY_GUARDED_DEADLINE_TRIGGER_SECS=330
POLY_GUARDED_REQUIRE_SIGNAL=armed
POLY_GUARDED_RUN_SECONDS=20
```

Além disso, o ambiente precisa conter as credenciais do broker real já usadas nos diagnósticos anteriores.

### 2.2 Preflight

```bash
python diagnostics_live_guard_preflight.py
```

Valida:
- se o broker env está pronto
- se o runner protegido está habilitado
- se o modo atual está pronto para runner real (`shadow_only=false`, `real_posts_enabled=true`)

### 2.3 Runner principal (real fill-cycle v2)

```bash
python run_guarded_bot.py --seconds 900
```

Valida:
- preflight + startup guard
- operação somente em `next_1`
- reconcile e sync contínuo no ciclo
- persistência JSON + cleanup do plano

## 3. Ordem recomendada de execução

### Em PC sem wallet
1. `git pull`
2. `python diagnostics_regression_suite_v1.py`

### Em PC com wallet / broker autenticado
1. `git pull`
2. `python run_guarded_bot.py --preflight-only`
3. `python run_guarded_bot.py --seconds 900`

## 3.1 Runner principal (real, next_1 only)

Para facilitar a execução no dia a dia (preflight + runner em um comando), use:

```bash
python run_guarded_bot.py --seconds 900
```

Fluxo esperado:
- imprime `[BROKER_ENV]` e `[LIVE_GUARDED_CONFIG]`
- valida guardrails de segurança
- imprime `[RESULT] Ready for live real fill-cycle monitoring (next_1 only).`
- inicia `monitor_live_real_fill_cycle_v2`
- opera apenas `next_1` (nunca `next_2`)
- reconcilia ordens abertas e mantém persistência JSON

Somente preflight (sem iniciar o monitor):

```bash
python run_guarded_bot.py --preflight-only
```

## 4. Checklist rápido

### Offline
- [ ] `git pull` atualizado
- [ ] suíte offline passando
- [ ] `PASS=4 FAIL=0`

### PC de casa
- [ ] `.env` configurado
- [ ] `POLY_GUARDED_ENABLED=true`
- [ ] `POLY_GUARDED_SHADOW_ONLY=false`
- [ ] `POLY_GUARDED_REAL_POSTS_ENABLED=true`
- [ ] `POLY_GUARDED_ALLOW_NEXT_2=false`
- [ ] preflight ok
- [ ] startup guard ok
- [ ] runner real operando apenas `next_1`

## 5. Estado atual do projeto

Já validado:
- reconciliação de ordens
- startup guard
- sync de `size_matched`
- cancelamento vindo do broker
- terminalização com `FORCE_CLOSE`
- limpeza de `active_plan_id`
- suíte offline consolidada

Restrições operacionais do rollout:
- manter startup guard habilitado
- não operar `next_2`
- manter `max_active_plans=1`
- manter reconcile + persistência ligados
