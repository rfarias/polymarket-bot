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

Runners reais ativos (outro PC — casa):
- `run_live_current_almost_resolved_real_v1.py` — almost resolved (principal)
- `market/live_current_almost_resolved_real_v1.py` — módulo EE integrado (lógica EE real fica AQUI, não em live_early_entry_real_v1.py)
- `run_live_next1_scalp_real_v1.py` — next1 scalp
- `run_guarded_bot.py` — fill-cycle next_1

Sempre usar o watchdog para iniciar runners reais:
- `.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -ArmEE -Qty 6 -RunSeconds 1800 -PollSeconds 0.5 -Continuous`

Nunca iniciar runner real diretamente com `python run_live_*.py --seconds <longo>` sem watchdog.

## Manutenção agendada Polymarket

**2026-05-27 12:10–12:20 UTC** (09:10–09:20 local UTC-3)

- Script `_pause_maintenance_20260527.ps1` roda em background
  - Para o runner às **09:05 local** (5 min antes)
  - Reinicia o watchdog às **09:25 local** (5 min após fim previsto)
- Loga em `logs/_maintenance_20260527.log`
- Para manutenções futuras: replicar o script ajustando os horários

## Estado da pesquisa EE (Early Entry)

Fase atual: **Monitoramento e ajuste fino** (desde 2026-05-27)

Gates deployados em 2026-05-27 (`market/live_current_almost_resolved_real_v1.py`):
- `n_s180 < 3` → bloqueia entrada (WR 44% → 83%)
- `secs > 155` → bloqueia entrada (faixa tardia WR 47%)
- Stop FAK removido (todos os 14 stops anteriores eram falsos; fill em livro fino)
- Proteção de reversão real: `ee_reversal` quando opp_bid >= 0.85 em secs <= 35

Pendente: validar efetividade dos gates com 50+ trades diurnos (semana de 2026-05-28).
- Gate candidato `el_vel >= 0.13`: só implementar após 100+ trades com WR > 85%
- Paper local (este PC): coleta seg-sex para validar o gate
- Logs reais ficam no outro PC em `logs/ee_real_*/ee_real.jsonl`

Para analisar logs reais no outro PC após `git pull`:
```powershell
.\_run_real_analysis.ps1 -Push   # roda todos os scripts + commita resultados
```

Scripts de análise disponíveis (todos aceitam `--logs "logs/ee_real_*/ee_real.jsonl"`):
- `_sim_el_flip.py` — simulação de inversão do EL (TESTE E — novo)
- `_sim_full_universe.py` — universo completo de candidatos EE
- `_sim_el_bid_stability.py` — estabilidade do bid antes da entrada
- `_sim_3gates.py` — gates de estabilidade (dip, mono, opp)
- `_sim_dip_strategies.py` — impacto de bloquear vs esperar dip
- `_sim_btc_momentum.py` — paradoxo adverso BTC

## Análise EL Flip (nova — 2026-05-25)

Inversão do Early Leader: quando o EL original perde a liderança e o lado oposto
assume com bid em [0.60, 0.72], entra-se no novo líder.

Resultado no paper local (fim de semana, 860 slugs):
- 41% dos slugs têm inversão na faixa
- Sem stop: WR 81%, avg +$0.276/trade
- Segmento gap >= 0.35: WR 89.5%, avg +$0.385
- Stop=0.45 causa 40% de falsos stops — não usar nesta estratégia

Pendente: validar nos logs reais (rodar `_sim_el_flip.py --no-stop` no outro PC).
Documentação completa: seção 15 de TESTES_ANALISE_EL.md

## Parâmetros de risco (não alterar sem confirmação)

- Qty padrão de teste: 6 shares
- Não avançar para 50 ou 100 shares sem validação nos logs reais
- `passive_capture_only` só alterar após comparar sessions reais vs paper
- Stop, target e hold_to_resolution são definidos pelo sinal — não sobrescrever no runner
- Gate el_vel >= 0.13: só implementar após 100+ trades de dias úteis com WR > 85%
- EL Flip: não implementar no runner real sem validação nos logs reais
- **EE stop FAK removido em 2026-05-27** — não reintroduzir sem nova análise de dados reais
