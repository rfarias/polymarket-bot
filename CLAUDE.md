# Bot Polymarket BTC 5min

## Regras

- Nunca alterar parâmetros de risco sem confirmar explicitamente
- Sempre comparar execução real vs paper antes de propor mudanças
- Logs ficam em /logs — analisar antes de qualquer correção
- A API da Polymarket usa autenticação L1/L2 — não quebrar esse fluxo

## Fluxo de trabalho autônomo

O agente pode prosseguir sem confirmação para:
- Análise de logs
- Paper trading (sem ordens reais)
- Replay histórico
- Melhorias de diagnóstico
- Documentação
- Commits de mudanças testadas
- Organização de handoff

O agente deve pedir confirmação antes de:
- Postar ordens reais
- Aumentar tamanho de mão
- Remover ou afrouxar travas de risco
- Alterar credenciais ou ambiente real
- Ligar autonomia de execução
- Apagar logs ou estado operacional

## Contexto do projeto

Runners reais ativos:
- `run_live_current_almost_resolved_real_v1.py` — almost resolved (principal agora)
- `run_live_next1_scalp_real_v1.py` — next1 scalp
- `run_guarded_bot.py` — fill-cycle next_1

Sempre usar o watchdog para iniciar runners reais:
- `.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 6 -RunSeconds 1800 -PollSeconds 0.5`

Nunca iniciar runner real diretamente com `python run_live_*.py --seconds <longo>` sem watchdog.

## Parâmetros de risco (não alterar sem confirmação)

- Qty padrão de teste: 6 shares
- Não avançar para 50 ou 100 shares sem validação nos logs reais
- `passive_capture_only` só alterar após comparar sessions reais vs paper
- Stop, target e hold_to_resolution são definidos pelo sinal — não sobrescrever no runner
