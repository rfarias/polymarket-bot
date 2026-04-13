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

Saída esperada no final:
- `TOTAL=3 PASS=3 FAIL=0`
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

## 2. Testes com broker autenticado (PC de casa)

Esses testes exigem `.env` configurado com credenciais válidas do broker real.

### 2.1 Pré-requisitos do `.env`

Exemplo mínimo para runners protegidos:

```env
POLY_GUARDED_ENABLED=true
POLY_GUARDED_SHADOW_ONLY=true
POLY_GUARDED_REAL_POSTS_ENABLED=false
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
- se o modo atual é `real-shadow`

### 2.3 Runner protegido com startup guard e sync

```bash
python -c "from market.live_minimal_guarded_v4 import monitor_live_minimal_guarded_v4; print('[TEST] Starting live minimal guarded v4...'); monitor_live_minimal_guarded_v4(duration_seconds=20); print('\n[TEST] live minimal guarded v4 finished 🚀')"
```

Valida:
- `BROKER_HEALTH`
- `BROKER_OPEN_ORDERS_STARTUP`
- `STARTUP_GUARD`
- snapshots do runner
- reconciliação e sync no ciclo

## 3. Ordem recomendada de execução

### Em PC sem wallet
1. `git pull`
2. `python diagnostics_regression_suite_v1.py`

### Em PC com wallet / broker autenticado
1. `git pull`
2. `python diagnostics_live_guard_preflight.py`
3. `python -c "from market.live_minimal_guarded_v4 import monitor_live_minimal_guarded_v4; print('[TEST] Starting live minimal guarded v4...'); monitor_live_minimal_guarded_v4(duration_seconds=20); print('\n[TEST] live minimal guarded v4 finished 🚀')"`

## 4. Checklist rápido

### Offline
- [ ] `git pull` atualizado
- [ ] suíte offline passando
- [ ] `PASS=3 FAIL=0`

### PC de casa
- [ ] `.env` configurado
- [ ] `POLY_GUARDED_ENABLED=true`
- [ ] preflight ok
- [ ] startup guard ok
- [ ] runner protegido em `shadow_only`

## 5. Estado atual do projeto

Já validado:
- reconciliação de ordens
- startup guard
- sync de `size_matched`
- cancelamento vindo do broker
- terminalização com `FORCE_CLOSE`
- limpeza de `active_plan_id`
- suíte offline consolidada

Ainda **não** liberado por padrão:
- postagem real de ordens
- flatten real em conta live
- execução live sem guardas

A intenção continua sendo evoluir primeiro a reconciliação/sync, mantendo `real_posts_enabled=false` por padrão até a camada real estar madura.
