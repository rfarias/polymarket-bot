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

Fase atual: **Melhorias de execução ativas** (2026-05-28)

### Gates deployados no runner real (acumulados)

`market/live_current_almost_resolved_real_v1.py`:

| Gate / Fix | Data | Base |
|------------|------|------|
| `n_s180 < 3` → bloqueia entrada | 2026-05-27 | WR 44% → 83% |
| `secs > 155` → bloqueia entrada | 2026-05-27 | faixa tardia WR 47% |
| Stop FAK removido | 2026-05-27 | 14/14 stops eram falsos; fill catastrófico |
| `ee_reversal` opp_bid >= 0.85 em secs <= 35 | 2026-05-27 | proteção de reversão genuína |
| **`EE_VEL_MIN 0.08 → 0.13`** | **2026-05-28** | **WR 68%→86% / avg −$0.22→+$0.44** |
| **Fix bug `el_bid=0` sem saída** | **2026-05-28** | **perda total quando livro EL esvaziava** |

### Aplicar no PC de casa (git pull pendente)

```powershell
git pull
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -ArmEE -Qty 6 -RunSeconds 1800 -PollSeconds 0.5 -Continuous
```

Confirmar após reinício: `entry_blocked` com reason `vel<0.13` deve aparecer (~30% das
entradas filtradas). Documentação completa: `TESTES_ANALISE_EL.md` seção 23.

### Gates candidatos no paper local (este PC)

`market/live_early_entry_paper_v1.py`:
- `n_s180 < 3` — replicado do real
- `n_s180 == 5` — WR 56.2%, validar 50+ dias úteis
- `hora UTC == 6` — WR 42.9%, validar consistência

### Pendente — zona morta 0.50–0.84 com secs > 35

Posições nessa faixa ainda não têm proteção. Fix planejado (stop suave se
`el_bid < 0.72 AND opp_bid > 0.72`), mas aguarda dados reais com os gates atuais
ativos. Não implementar sem análise dos logs reais pós-pull.

### Para analisar logs reais no outro PC após `git pull`

```powershell
.\_run_real_analysis.ps1 -Push   # roda todos os scripts + commita resultados
```

Scripts disponíveis (aceitam `--logs "logs/ee_real_*/ee_real.jsonl"`):
- `_sim_el_flip.py` — simulação de inversão do EL
- `_sim_full_universe.py` — universo completo de candidatos EE
- `_sim_el_bid_stability.py` — estabilidade do bid antes da entrada
- `_sim_3gates.py` — gates de estabilidade (dip, mono, opp)
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

## Diagnóstico de regime AR (implementado 2026-05-28)

Campo `regime_diagnostics` adicionado ao evento `enter` do AR paper (`diagnostics_current_almost_resolved_paper_v1.py`):
- `risky_zone=true` quando: `variant==standard` AND `dist_bps < 14` AND `range60 > 0.15`
- Base: 23 losses AR em 838 trades — ~12 seriam capturados por esse critério
- Não bloqueia — apenas sinaliza para análise prospectiva
- Gate real só após 50+ entradas com `risky_zone=true` confirmadas como losses

## Parâmetros de risco (não alterar sem confirmação)

- Qty padrão de teste: 6 shares
- Não avançar para 50 ou 100 shares sem validação nos logs reais
- `passive_capture_only` só alterar após comparar sessions reais vs paper
- Stop, target e hold_to_resolution são definidos pelo sinal — não sobrescrever no runner
- **`EE_VEL_MIN = 0.13` deployado em 2026-05-28** — não baixar sem nova análise
- EL Flip: não implementar no runner real sem validação nos logs reais
- **EE stop FAK removido em 2026-05-27** — não reintroduzir sem nova análise de dados reais
- Gates EE candidatos (n_s180=5, hora=6h UTC): só ir pro real após validação no paper (50+ dias úteis)
- Stop suave zona 0.50–0.84: não implementar sem análise dos logs reais pós-pull
