# polymarket-bot

Base project for a BTC Polymarket bot.

Documentação de handoff:

- [README_ARCHITECTURE.md](/C:/Users/Letícia/Documents/polymarket-bot/README_ARCHITECTURE.md)
- [README_NEXT1_SCALP.md](/C:/Users/Letícia/Documents/polymarket-bot/README_NEXT1_SCALP.md)
- [README_NEXT1_SCALP_REAL_VALIDATION.md](/C:/Users/Letícia/Documents/polymarket-bot/README_NEXT1_SCALP_REAL_VALIDATION.md)
- [README_NEXT1_FILL_CYCLE.md](/C:/Users/Letícia/Documents/polymarket-bot/README_NEXT1_FILL_CYCLE.md)
- [README_CURRENT_SETUPS.md](/C:/Users/Letícia/Documents/polymarket-bot/README_CURRENT_SETUPS.md)
- [README_SCALP_REVERSAL.md](/C:/Users/Letícia/Documents/polymarket-bot/README_SCALP_REVERSAL.md)
- [README_ROADMAP_MULTI_REAL.md](/C:/Users/Letícia/Documents/polymarket-bot/README_ROADMAP_MULTI_REAL.md)
- [README_CONVENTIONS.md](/C:/Users/Letícia/Documents/polymarket-bot/README_CONVENTIONS.md)

## Quickstart (Git Bash / local)

```bash
pip install -r requirements.txt
cp .env.example .env
python run_guarded_bot.py --preflight-only
python run_guarded_bot.py --seconds 900
```

Veja `README_TESTS.md` para a trilha completa de testes offline e live guardado.

Runner de scalp reversal:

```bash
python run_scalp_reversal_bot.py --preflight-only
python run_scalp_reversal_bot.py --seconds 300
```

Runner real de `next1 scalp`:

```bash
python run_live_next1_scalp_real_v1.py --preflight-only
python run_live_next1_scalp_real_v1.py --seconds 1200
```

Para rodar o `next1 scalp` real por mais tempo, use o watchdog no PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\watch_next1_scalp_real.ps1
```

Por padrão ele roda ciclos de 6h (`21600` segundos), sobrescreve `POLY_NEXT1_SCALP_RUN_SECONDS` apenas no processo do runner e reinicia após cada encerramento. Isso evita que o runner pare definitivamente quando atingir o tempo configurado no `.env` (`POLY_NEXT1_SCALP_RUN_SECONDS=900`) ou quando uma exceção derrubar o ciclo. Logs principais:

- `logs\next1_scalp_real_watchdog_*.log`: início/fim de cada ciclo e código de saída.
- `logs\next1_scalp_real_*\next1_scalp_real.jsonl`: snapshots, entradas, saídas, cancelamentos, estado e eventos de risco.
- `logs\next1_scalp_real_*\exception.log`: traceback quando o ciclo para por exceção.
- `logs\next1_scalp_real_state.json`: estado persistido quando há posição/ordem pendente; se existir, o próximo ciclo tenta restaurar antes de operar.

Motivos conhecidos para o runner real encerrar:

- Fim normal do tempo de execução (`--seconds` ou `POLY_NEXT1_SCALP_RUN_SECONDS`).
- Guardas de startup bloqueando credenciais, modo real, healthcheck ou ordens externas abertas.
- Estado restaurado não-idle que não pôde ser limpo com segurança.
- Exceção de broker/API; nesses casos o runner grava `exception.log`, tenta limpeza de risco, salva estado e encerra para o watchdog reiniciar.

Runner real de `current scalp`:

```bash
python run_live_current_scalp_real_v1.py --preflight-only
python run_live_current_scalp_real_v1.py --seconds 1800
```

Runner real de `current almost resolved`:

```bash
python run_live_current_almost_resolved_real_v1.py --preflight-only
python run_live_current_almost_resolved_real_v1.py --seconds 300
```

Esse runner fica armado apenas com `POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED=true`. A primeira versao real e dedicada ao setup de quase resolvidos, bloqueia startup se houver ordens abertas e nao deve ser executada junto com o `next1 scalp real` enquanto a validacao simultanea ainda nao estiver pronta.
Residual microscopico abaixo de `POLY_CURRENT_ALMOST_RESOLVED_DUST_ARCHIVE_QTY` e arquivado como poeira para nao travar o runner em `pending_exit`.

### CLOB V2 e allowance

Em maio de 2026, o CLOB V1 nao aceita mais ordens novas e retorna `order_version_mismatch`. O broker real (`market/polymarket_broker_v3.py`) tenta usar `py-clob-client-v2` primeiro e so cai para o cliente antigo como compatibilidade local.

Antes de rodar qualquer runner real, confirme que a conta tem allowance no Exchange V2/pUSD:

```bash
python -c "from market.polymarket_broker_v3 import PolymarketBrokerV3; b=PolymarketBrokerV3.from_env(); print(b.get_balance_allowance(asset_type='COLLATERAL'))"
```

O spender `0xE111180000d2663C0091e4f400237545B87B996B` precisa ter allowance positivo. Se estiver `0`, a API rejeita a entrada com `not enough balance / allowance` mesmo havendo saldo. Depois de aprovar pela UI/onramp, o preflight deve continuar mostrando `BROKER_OPEN_ORDERS_STARTUP []`.

Sessao real de validacao em 2026-05-14:

- V1 falhou com `order_version_mismatch`.
- V2 passou pelo preflight e pela validacao de versionamento.
- Apos aprovacao do allowance V2, a sessao `logs/current_almost_resolved_real_20260514_124635/` rodou ate o encerramento da janela sem `allow=True`, sem `enter`, sem `fill`, sem `exception` e sem open orders finais.

Para smoke curto com reinicio automatico:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\watch_current_almost_resolved_real.ps1 -RunSeconds 300 -PollSeconds 0.5 -Qty 6
```

Settlement de portfólio (`merge` + `redeem` / claim, opcional e desativado por padrão):

```bash
python run_portfolio_settlement_v1.py --preflight-only
python run_portfolio_settlement_v1.py --seconds 3600
```

Watchdog:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\watch_portfolio_settlement.ps1 -RunSeconds 3600 -PollSeconds 60
```

Esse processo é separado dos runners de trading e deve continuar desligado enquanto o auto redeem do portfólio estiver suficiente. Ele varre posições `mergeable` e `redeemable`, tenta primeiro `merge` de pares completos e depois `redeem` das vencedoras resolvidas, tudo via relayer da carteira proxy/safe. Em 23 de abril de 2026, a documentação oficial lista `Conditional Tokens = 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` e `pUSD = 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`; esses valores ficam parametrizados por env para evitar acoplamento rígido.

Importante: no ambiente atual, as credenciais de trading `POLY_API_*` autenticam no CLOB, mas o relayer respondeu `401 invalid authorization`. Portanto, para execução real do settlement, configure `POLY_BUILDER_API_KEY`, `POLY_BUILDER_API_SECRET` e `POLY_BUILDER_PASSPHRASE` próprios do relayer/builder. O preflight já acusa isso claramente.
