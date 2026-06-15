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

**Contexto**: o runner real acumula ~25 entradas EE/dia com vel>=0.13. Após ~30 dias,
esperamos 750–900 entradas com `el_bid`, `el_vel`, `n_s180`, `ep`, `secs` e PnL real —
suficiente para validar todos os gates candidatos sem precisar de Telonex.

#### Passo 1 — sincronizar

```powershell
git pull
```

Verificar quantidade de logs disponíveis:

```powershell
Get-ChildItem logs\ee_real_* -Directory | Measure-Object  # quantas sessões
Get-ChildItem logs\ee_real_*\ee_real.jsonl | ForEach-Object { (Get-Content $_ | Measure-Object -Line).Lines } | Measure-Object -Sum
```

#### Passo 2 — pipeline de simulações existentes

```powershell
.\_run_real_analysis.ps1 -Push   # roda todos os scripts + commita resultados
```

Scripts disponíveis (aceitam `--logs "logs/ee_real_*/ee_real.jsonl"`):
- `_sim_el_flip.py` — simulação de inversão do EL
- `_sim_full_universe.py` — universo completo de candidatos EE
- `_sim_el_bid_stability.py` — estabilidade do bid antes da entrada
- `_sim_3gates.py` — gates de estabilidade (dip, mono, opp)
- `_sim_btc_momentum.py` — paradoxo adverso BTC

#### Passo 3 — validação retroativa dos gates candidatos (vel, n_s180, ep)

Gates testados no paper mas ainda não deployados no real: `vel>=0.17`, `n_s180>=6`, `ep<=0.85`.
Este script aplica cada gate retroativamente sobre os trades reais fechados e compara WR/PnL:

```python
# _analise_gates_real.py  (rodar no outro PC)
import json, pathlib, statistics

LOGS = sorted(pathlib.Path("logs").glob("ee_real_*/ee_real.jsonl"))

entries, closed_map = {}, {}
for f in LOGS:
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
            t = ev.get("type", "")
            slug = ev.get("slug", "")
            if t == "enter":
                el = ev.get("el", {})
                entries[slug] = {
                    "vel": el.get("el_vel"),
                    "n_s180": el.get("n_s180"),
                    "ep": ev.get("ep"),
                    "secs": ev.get("secs"),
                    "el_bid": el.get("el_bid_180"),
                    "variant": ev.get("setup_variant"),
                }
            elif t in ("trade_closed", "flat", "redeem_flat"):
                closed_map[slug] = ev.get("pnl") or ev.get("result", {}).get("pnl")
        except:
            pass

trades = [(entries[s], closed_map[s]) for s in entries if s in closed_map and closed_map[s] is not None]
print(f"Total trades fechados: {len(trades)}")

def stats(subset, label):
    pnls = [pnl for _, pnl in subset]
    if not pnls:
        print(f"  {label}: sem dados"); return
    wins = [p for p in pnls if p > 0]
    wr = len(wins) / len(pnls) * 100
    avg = statistics.mean(pnls)
    print(f"  {label}: n={len(pnls)}  WR={wr:.1f}%  avg={avg:+.3f}")

print("\n=== SEM FILTRO (baseline) ===")
stats(trades, "todos")

print("\n=== GATE vel >= 0.17 ===")
stats([(e, p) for e, p in trades if e.get("vel") is not None and e["vel"] >= 0.17], "vel>=0.17")
stats([(e, p) for e, p in trades if e.get("vel") is not None and e["vel"] < 0.17], "vel<0.17 (bloqueado)")

print("\n=== GATE n_s180 >= 6 ===")
stats([(e, p) for e, p in trades if e.get("n_s180") is not None and e["n_s180"] >= 6], "n_s180>=6")
stats([(e, p) for e, p in trades if e.get("n_s180") is not None and e["n_s180"] < 6], "n_s180<6 (bloqueado)")

print("\n=== GATE ep <= 0.85 ===")
stats([(e, p) for e, p in trades if e.get("ep") is not None and e["ep"] <= 0.85], "ep<=0.85")
stats([(e, p) for e, p in trades if e.get("ep") is not None and e["ep"] > 0.85], "ep>0.85 (bloqueado)")

print("\n=== TODOS OS GATES (vel>=0.17 + n_s180>=6 + ep<=0.85) ===")
stats([(e, p) for e, p in trades
       if e.get("vel", 0) >= 0.17 and e.get("n_s180", 0) >= 6 and e.get("ep", 1) <= 0.85], "todos os gates")

print("\n=== HORA UTC ===")
import datetime
for h in [6, 7, 8, 9, 10, 11, 12, 13, 14]:
    hora_trades = [(e, p) for e, p in trades
                   if datetime.datetime.utcfromtimestamp(e.get("secs", 0) or 0).hour == h]
    # nota: usar ts da entry, nao secs
# (adaptar se entry tiver campo 'ts' separado)
```

Critério para promover ao real: gate candidato deve mostrar **WR >= 85% e n >= 50** nos logs reais.

#### Passo 4 — validação WR do extreme_99_limit

```python
# trecho a acrescentar em _analise_gates_real.py
print("\n=== EXTREME_99_LIMIT ===")
ext = [(e, p) for e, p in trades if e.get("variant") == "extreme_99_limit"]
stats(ext, "extreme_99_limit")
# EV > 0 exige WR > 99% (win=$0.01, loss=$0.99). Não operar sem n>=200 confirmados.
```

#### Passo 5 — SA2 retroativo (qualidade de entrada)

Estima quanto cada trade teria poupado entrando 1 tick abaixo do `el_bid_180`:

```python
# trecho a acrescentar em _analise_gates_real.py
sa2 = [(e, p) for e, p in trades if e.get("el_bid") is not None]
savings = [e["el_bid"] - (e["el_bid"] - 0.01) for e, _ in sa2]  # = sempre $0.01/share
win_saves = [0.01 * 6 for e, p in sa2 if p > 0]  # economy por win (qty=6)
# SA2 fill rate (bid caiu 1 tick antes de resolver?) só verificável com Telonex ou livro real
print(f"\n=== SA2 RETROATIVO ===")
print(f"Trades com el_bid disponivel: {len(sa2)}")
print(f"Economia potencial SA2 (se 88% fill wins): {len([p for _,p in sa2 if p>0]) * 0.01 * 6 * 0.88:.2f} USD")
```

#### Passo 6 — commitar resultados

```powershell
git add _result_*.csv _result_*.json
git commit -m "analise: gates reais pos-pull $(Get-Date -Format 'yyyy-MM-dd')"
git push
```

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

## Backtest externo — BrockMisner dataset (2026-06-04)

Dataset: `BrockMisner/polymarket-btc-updown` (HuggingFace, gratuito)
Período: fev–abr 2026 | Scripts: `_backtest_brockMisner.py`, `_backtest_bid_prices.py`

### Resultados EE (Early Entry)

| Fonte de preço | Mercados | Sinais | WR | IC 95% | p-valor |
|---------------|----------|--------|----|--------|---------|
| Mid-price (`up_price`) | 5.817 | 37 | **94.6%** | [82%, 98%] | 5×10⁻⁹ ✓ |
| Bid real (`best_bid`) | 616 | 3 | 100% | [44%, 100%] | n.s. |

**Interpretação EE:** a queda de 37→3 sinais é proporcional ao sample menor (616/5817 ≈ 10.6% → 37×10.6% ≈ 3.9 esperado). WR consistente com os logs paper (88.7%) e com o resultado mid-price (94.6%). Bid real ≈ mid − 0.01, como esperado.

### Resultados AR (Almost Resolved) — bid real

| Faixa de bid | secs máx | n | WR |
|-------------|----------|---|----|
| bid ≥ 0.95 | 60s | 252 | **99.2%** |
| bid ≥ 0.90 | 60s | 170 | 90.6% |
| bid ≥ 0.88 | 60s | 600 | **94.0%** IC=[91.8%,95.6%] p=2×10⁻¹²³ ✓ |
| bid ≥ 0.75 | 120s | 613 | 85.5% |
| bid 0.75–0.80 | 120s | 241 | 78.4% |

**Interpretação AR:** o resultado de 94% com bid real (vs 100% trivial com mid-price) é o dado mais valioso. Confirma que ~6% dos mercados revertem mesmo com bid ≥ 88% nos últimos 60s. Gradiente claro: quanto maior o bid e menor o secs, maior o WR.

### Caveats

- Período (fev–abr 2026) coincide com calibração das gates EE → possível look-ahead parcial
- `n_s180` recalibrado para granularidade do dataset (≥3 em vez de ≥6)
- Para validação limpa: precisaria de dados pós-junho 2026 com bid real

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
