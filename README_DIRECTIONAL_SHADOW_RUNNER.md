# Directional Shadow Runner

Este runner simula os setups direcionais com friccao mais proxima do runner real, mas sem postar ordens.

Arquivos:
- `diagnostics_directional_shadow_runner_v1.py`
- `run_directional_shadow_runner_v1.py`
- `start_directional_shadow_runner.ps1`

Setups cobertos:
- `current_scalp`
- `current_almost_resolved`
- `next1_scalp`

## O que muda em relacao ao paper otimista

Este runner nao considera fill instantaneo so porque o preco tocou.

Ele aplica:
- entrada em `pending_entry`
- minimo de polls favoraveis antes do fill
- idade minima da ordem antes de confirmar o fill
- timeout de entrada por setup
- `reprice` de entrada
- saida em `pending_exit`
- `reprice` de saida
- `force exit` perto do fim com penalidade de slippage

Isso nao replica a fila real da exchange perfeitamente, mas fica muito mais perto do runner real do que os papers antigos.

## Como rodar

Execucao direta:

```bash
python run_directional_shadow_runner_v1.py --seconds 3600 --poll-secs 1.0 --log-dir logs\directional_shadow_runner_1h
```

Launcher pronto para deixar rodando:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_directional_shadow_runner.ps1 -Hours 8 -PollSecs 1.0
```

Se quiser reiniciar automaticamente em caso de erro:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_directional_shadow_runner.ps1 -Hours 8 -PollSecs 1.0 -RestartOnFailure
```

## Onde ficam os logs

O launcher cria uma pasta de sessao em:

```text
logs\directional_shadow_sessions\<timestamp>\
```

Arquivos esperados:
- `directional_shadow_runner.jsonl`: snapshots e eventos
- `session_summary.json`: resumo final
- `runner_stdout.log`: saida padrao
- `runner_stderr.log`: erros
- `session_meta.json`: parametros usados

## O que olhar quando voltar

Primeiro:
- `session_summary.json`

Campos principais:
- `completed_trades`
- `wins`
- `losses`
- `flat`
- `total_pnl_ticks`
- `by_setup`
- `blocked_reasons`

Depois:
- `directional_shadow_runner.jsonl`

Tipos de evento mais importantes:
- `entry_posted`
- `entry_filled`
- `entry_repriced`
- `entry_timeout`
- `entry_signal_invalidated`
- `exit_posted`
- `exit_filled`
- `exit_repriced`
- `exit_forced`
- `exit`

## Observacoes operacionais

- O PC precisa ficar acordado, com rede e sem suspensao.
- Se a rede cair ou a API da Polymarket falhar, a qualidade da sessao cai.
- Este runner ainda nao cobre o hedge `next_1 fill-cycle`.
- Para o hedge, o ideal e criar um shadow runner separado de multi-leg.
