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

Fase atual: **Melhorias de execução ativas** (2026-06-01)

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
| **PP removido — hold to resolution** | **2026-05-27** | **PP fill parcial 65% → negativo vs hold** |
| **Gate spread>=0.70 removido** | **2026-06-01** | **redundante com vel>=0.17; bloqueia wins bons** |
| **`EE_VEL_MIN 0.13 → 0.17` (paper)** | **2026-06-01** | **WR 88.7%, EV+ em ep 0.83–0.86** |
| **SA2 bid passivo ep-0.01 (paper)** | **2026-06-01** | **reduz seleção adversa 75%→88% fill wins** |
| **`EE_ENTRY_HI 0.86 → 0.85` (paper)** | **2026-06-01** | **ep=0.86 tinha WR=58%, EV=-0.277/share (_sim_new_setups)** |
| **`n_s180 < 6` bloqueia (paper)** | **2026-06-01** | **n_s180=3-5 WR=65-67%; n_s180=7 WR=95% (_sim_new_setups)** |

### SA2 — Bid passivo (implementado no paper 2026-06-01, pendente no real)

**O problema**: wins preenchem 75% (mercado sobe rápido, livro fino).
Losses preenchem 100% (seleção adversa). Custo: -$18/10d em qty=6.

**A solução SA2**: posta bid a `el_bid − 0.01` (passivo/maker) em vez de entrar imediatamente.
O mercado recua 1 tick naturalmente → fill passivo.

**Resultado simulado** (2529 slugs): fill 88% wins, entrada 1 tick mais barata → **+$33/10d** vs hold atual.

**Para implementar no runner real (casa)**:
Ao detectar sinal EE, em vez de `place_order(side, price=el_bid)`:
1. Postar GTC limit buy a `el_bid − 0.01`
2. Monitorar até fill (el_bid cai até o preço) ou cancelamento
3. Cancelar se: secs < 35 OU mercado subiu > 5 ticks sem fill OU side mudou

Não usar stop (stop mata 15.4% dos wins — confirmado 91 wins, floor real = 0.15).
Não usar PP (PP negativo com fill parcial 65% — confirmado).

### Gates candidatos no paper local (este PC)

`market/live_early_entry_paper_v1.py` (active):
- vel >= 0.17 (EE_VEL_MIN)
- `n_s180 < 6` — deployado no paper 2026-06-01 (n_s180=3-5 WR=65-67%, n_s180=7 WR=95%)
- `EE_ENTRY_HI = 0.85` — deployado no paper 2026-06-01 (ep=0.86 WR=58%)
- `hora UTC == 6` — candidato: WR 42.9% (7 trades)

### Pendente — zona morta 0.50–0.84 com secs > 35

Stop não funciona (mata 15.4% wins). Zona ainda sem proteção.

### Pendente — validação EV do setup extreme_99_limit

**Problema**: EV > 0 exige WR > 99.0% (win=$0.01/share, loss=$0.99/share).
O mercado precifica reversão em ~1–3% (opp=0.01–0.03) — se correto, EV já é negativo.

**Dados locais**: apenas 9 eventos, nenhum trade fechado. Insuficiente para estimar WR.

**Ação**: após `git pull` no outro PC, rodar análise de trades fechados por variante nos logs reais:
```python
# filtrar type in ('trade_closed','flat','redeem_flat') AND setup_variant == 'extreme_99_limit'
# calcular WR, avg_pnl, n_reversoes
```
Não escalar nem recomendar operação manual do extreme_99 sem WR empírico confirmado > 99%.
Aguarda dados SA2 no paper + logs reais pós-pull para decidir.

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

## EV Scanner — módulo complementar (stand by desde 2026-06-04)

Arquivo principal: `ev_scanner/main.py`
Filosofia: comparar probabilidade real (fonte externa) vs preço Polymarket → entrar se edge ≥ 8%.
Todos os setups operam em **paper only** — nenhuma ordem real.

Para rodar: `python -m ev_scanner.main` (loop) ou `--once` (uma vez) ou `--setup <nome>`.

### Estado atual dos setups

| Setup | Fonte de prob_real | active | Motivo |
|-------|-------------------|--------|--------|
| `weather` | Open-Meteo climatologia histórica 20 anos | `true` | funcional |
| `nba_nfl` | nba_api PyPI (sem chave) | `true` | funcional |
| `fed_rate` | CME FedWatch | `false` | retorna 403 neste PC |
| `soccer` | SofaScore (unofficial) | `false` | retorna 403 neste PC |

### Correções aplicadas (2026-06-04)

- **Logs contaminados arquivados** em `ev_scanner/logs/_archive/` — modelos errados de 02–04/06
- **soccer dedup** corrigido — `open_keys + entered_this_run` (mesmo padrão do nba_nfl)
- **weather timing** corrigido — `min_days_ahead=1` trocado por `min_hours_ahead=14.0`:
  endDate dos mercados de temperatura é `T12:00:00Z`; divisão inteira bloqueava
  mercados de amanhã com 17h restantes (`int(17/24)=0`).
- **weather model** reescrito (commit 8dd9ceb): prob_real = frequência empírica de 20 anos
  de arquivo Open-Meteo (±7 dias do mesmo dia do ano), em vez de NWP forecast circular.

### Para reativar fed_rate

CME FedWatch retorna 403 neste PC (bloqueio de IP/geo).
Quando acessível, setar `"active": true` em `ev_scanner/config.json`.
O scan abortará limpo se o endpoint voltar a bloquear — sem fallback de base rates.

### Para reativar soccer

SofaScore retorna 403 neste PC. Alternativas a implementar (escolher uma):
1. **football-data.org** — gratuito com chave; adicionar `FOOTBALL_DATA_API_KEY` no `.env`;
   suporta EPL/La Liga/Série A/Bundesliga/Brasileirão/CL no tier free.
2. **ELO estático** — arquivo de ratings FIFA/ELO para seleções nacionais (Copa do Mundo);
   ESPN API tem jogos futuros mas sem histórico suficiente de classificatórias.

Enquanto SofaScore estiver bloqueado, todo scan cai para `form_source='market_prior'`
(circular — usa o próprio preço Polymarket como prior → nunca gera EV+ real).

### Critério para avançar para paper real → real

Por setup, após 50+ trades **resolvidos**:
- `edge_realizado >= 0.06`
- `win_rate >= prob_media_entrada × 0.90`
- `ev_por_trade >= $0.50`
- sem viés sistemático de lado

## Parâmetros de risco (não alterar sem confirmação)

- Qty padrão de teste: 6 shares
- Não avançar para 50 ou 100 shares sem validação nos logs reais
- `passive_capture_only` só alterar após comparar sessions reais vs paper
- Stop, target e hold_to_resolution são definidos pelo sinal — não sobrescrever no runner
- **`EE_VEL_MIN = 0.17` no paper desde 2026-06-01** — não baixar sem nova análise
- EL Flip: não implementar no runner real sem validação nos logs reais
- **EE stop FAK removido em 2026-05-27** — não reintroduzir sem nova análise de dados reais
- Gates paper `n_s180<6` e `EE_ENTRY_HI=0.85`: só ir pro real após 50+ trades no paper confirmando melhora de WR
- Gate `hora UTC==6`: só ir pro real após validação no paper (50+ dias úteis)
- Stop suave zona 0.50–0.84: não implementar sem análise dos logs reais pós-pull
