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
