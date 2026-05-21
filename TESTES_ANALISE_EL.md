# Early Leader — Análise, Simulações e Runner de Teste

Data da análise: 2026-05-21  
Logs analisados: 661 slugs resolvidos (monitor + paper + runner logs locais)  
Scripts de análise: `analyze_early_leader_all_logs.py`, `analyze_entry_threshold_and_gaps.py`, `analyze_monitor_pnl.py`  
Scripts de simulação: `_sim_early_entry.py`, `_sim_early_entry_v2.py`  
Runner de teste: `run_market_monitor.py` (Early Entry implementado)

---

## 1. O que é o Early Leader (EL)

O mercado BTC 5-min da Polymarket tem dois lados: UP e DOWN. Nos primeiros ~60 segundos do candle (secs 300→240), os bids costumam estar próximos de 0.50. A partir de secs 240–181, um dos lados começa a dominar o orderbook — esse é o **Early Leader (EL)**.

**Definição técnica:**
- Janela de detecção: secs 181–240
- Threshold mínimo: bid médio >= 0.55 no lado dominante
- O lado com bid médio >= 0.55 nessa janela é o `early_leader`

---

## 2. Poder Preditivo do EL

Amostra: 661 slugs resolvidos (todos os logs disponíveis em 2026-05-21)

| Condição | n | Acerto direcional |
|---|---|---|
| EL >= 0.55 (qualquer) | 545 / 661 | 79,1% |
| EL >= 0.65 | 376 | 83,8% |
| EL >= 0.70 | 296 | 87,8% |
| EL + F3 continuidade | 299 | **94,3%** |

**F3 — Filtro de continuidade:**  
O bid do lado EL nunca cai abaixo de 0.70 na janela secs 180–121.  
É o filtro de maior qualidade disponível e o gate central de toda estratégia EE.

---

## 3. Inversão do EL como Sinal de Reversão

Quando o lado dominante troca entre a janela 240–181s e uma janela posterior:

| Situação | n | EL original acerta | Novo líder acerta |
|---|---|---|---|
| EL estável até secs 60 | 445 | **92,6%** | 7,4% |
| EL inverte em 180–121s | 38 | 18,4% | **81,6%** |
| EL inverte em 120–61s | 62 | 19,4% | **80,6%** |

**Conclusão:** a inversão é um sinal de reversão tão forte quanto o EL original é sinal de continuidade. O novo líder vence ~81% das vezes.

Por intensidade do bid no momento da inversão:

| Bid no momento da inversão | n | Novo líder vence |
|---|---|---|
| < 0.60 (EL fraco inverteu) | 34 | 68% |
| 0.60–0.72 (EL médio inverteu) | 41 | **90%** |
| >= 0.72 (EL forte inverteu) | 25 | **84%** |

**Sinal mais forte:** inversão com bid 0.60–0.72 → 90% new leader wins.

---

## 4. Hipótese Early Entry (EE) — Comprar Mais Barato com EL Estável

### 4.1 Motivação

AR normal entra quando bid >= 0.88 (secs <= 120). Com EL estável (94% de acerto direcional), é possível entrar mais cedo (secs 121–180, bid 0.82–0.86) e ganhar mais por trade vencedor. A lógica: se a direção está quase garantida, entrar mais barato significa maior lucro por win.

### 4.2 Problema com stop curto

Com stop=2T (padrão AR): o mercado oscila naturalmente na faixa 0.82–0.86 antes de resolver. Parar muito cedo só captura ruído.

Drawdown natural de entradas EE (EL estável, bid 0.82–0.86):
- P25: 0.00 (25% das entradas não recuam nada)
- P50: 0.01 (mediana = 1 cent de drawdown)
- P75: 0.08
- P90: 0.15
- P95: 0.28

**Conclusão:** stop curto (2T–8T) mata trades bons. O stop ideal é nenhum, ou muito largo (hold to resolution).

### 4.3 Resultados da simulação — `_sim_early_entry.py`

Parâmetros: EL estável (F3 + el_vel >= 0.08), ep 0.82–0.86, qty=6, entry_secs_max=180.  
Amostra: 156 trades (de 661 slugs, ~23% com sinal válido).

| Stop | n | WR% | PnL total | avg/trade |
|---|---|---|---|---|
| 2T | 156 | 50,6% | +$45,60 | +$0,292 |
| 5T | 156 | 62,2% | +$43,50 | +$0,279 |
| 8T | 156 | 65,4% | +$44,10 | +$0,283 |
| 10T | 156 | 68,6% | +$44,70 | +$0,287 |
| 12T | 156 | 69,9% | +$46,20 | +$0,296 |
| **Sem stop** | **156** | **90,4%** | **+$59,64** | **+$0,382** |

Sem stop: 10 reversões (6,4%), avg loss = -$4,99.  
Comparativo: AR normal (ep>=0.88, stop 2T) → avg=+$0,102/trade. EE sem stop é **3,7x melhor por trade**.

---

## 5. Hedge em 0.50 — Proteção nas Reversões

### 5.1 O problema

Quando o EL falha (6–10% dos casos), o bid cruza abaixo de 0.50 e o lado oposto passa a dominar. Sem saída, a perda é ~-$5.06 (ep=0.84 → 0, qty=3 ou pior com qty=6).

### 5.2 A solução

Quando o bid EL cai abaixo de 0.50, **comprar o lado oposto** ao mesmo preço de mercado. O lado que vai vencer já está barato (~0.27) e vai a 1.0.

**Economia por reversão:**
- Sem hedge: média -$5,06
- Com hedge: média -$0,63
- Saving por reversão: +$4,43

**Custo do hedge em bounces** (casos onde EL recuperou): -$4,00 total em 6 bounces.

### 5.3 Resultado da estratégia completa (simulação com qty=3)

Amostra: 157 trades EE válidos.

| Estratégia | avg/trade | PnL total |
|---|---|---|
| Hold to resolution (sem hedge) | +$0,385 | +$60,48 |
| Sair em 0.50 | +$0,391 | +$61,44 |
| **Hedge em 0.50** | **+$0,718** | **+$112,68** |

**O hedge em 0.50 melhora 87% o avg/trade vs hold sem hedge.**

---

## 6. Filtros Adicionais Testados — `_sim_early_entry_v2.py`

### Filtro 1 — el_bid_180 (bid médio na janela 181–121s)

| Threshold | n | WR% | avg/trade |
|---|---|---|---|
| >= 0.70 (F3 base) | 157 | 90,4% | +$0,385 |
| >= 0.83 | 119 | 91,6% | +$0,4427 |

Melhora marginal. Não compensa a redução de amostra isoladamente.

### Filtro 2 — el_vel (crescimento do EL bid: bid_180 − bid_240)

| Threshold | n | WR% | avg/trade |
|---|---|---|---|
| >= 0.00 (qualquer positivo) | 144 | 91,7% | +$0,4554 |
| >= 0.05 | 126 | 92,1% | +$0,4776 |
| **>= 0.08** | **104** | **94,2%** | **+$0,6127** |
| >= 0.10 | 85 | 94,1% | +$0,6120 |

**el_vel >= 0.08 é o melhor filtro individual.** Reduz amostra de 157→104 mas aumenta WR de 90,4% para 94,2% e avg de +$0,38 para +$0,61.

### Filtro 3 — spot_delta_15s (variação do preço spot BTC nos últimos 15s)

| Condição | n | WR% | avg/trade |
|---|---|---|---|
| Spot CONCORDA com EL | 61 | 93,4% | +$0,5666 |
| Spot DISCORDA do EL | 41 | 82,9% | -$0,0717 |

Quando spot vai contra o EL, pular o trade. Melhora mais ainda se combinado com el_vel >= 0.08.

### Filtro 4 — Janela de entrada (entry secs)

| Janela | n | WR% | avg/trade |
|---|---|---|---|
| secs 121–180 | 140 | 91,4% | +$0,4414 |
| secs 91–120 | 8 | 75,0% | -$0,4500 |

Evitar entrar muito tarde (< 120s). Janela ideal: 121–180s.

### Combinação recomendada

**EL estável (F3) + el_vel >= 0.08 + hedge em 0.50**  
WR esperado: ~94%, avg/trade: ~$0,61 (qty=3, sem stop).

---

## 7. Book Gaps — Quedas Bruscas de Preço

### 7.1 Natureza dos gaps

Não são ausência de liquidez. O depth médio antes do gap é 37.000 shares (alto). São **reversões bruscas em 1 poll** (0.5s).

### 7.2 Estatísticas (n=495 quedas identificadas)

| Campo | Valor |
|---|---|
| Drop médio | 0,176 (17,6 cents) |
| Drop máximo | 0,760 |
| secs médio | 165s (acontecem em qualquer janela) |
| Depth médio antes | 37.092 |
| range_15s médio antes | **0,0699** |
| Velocidade 30s antes | **positiva** (+0,0033) — bid subia antes de cair |

### 7.3 Gate proposto: range_15s >= 0.03

Captura **67%** das quedas grandes (drop >= 0.10) quando o campo está presente.  
Falsos bloqueios (trades bons bloqueados): ainda não medido nos logs do runner real.

---

## 8. Implementação no Runner de Teste — `run_market_monitor.py`

### 8.1 Constantes adicionadas

```python
EE_EL_MIN    = 0.55   # bid mínimo para detectar EL em 240–181s
EE_CONT_MIN  = 0.70   # bid mínimo de continuidade do EL em 180–121s (F3)
EE_VEL_MIN   = 0.08   # crescimento mínimo do bid EL (bid_180 − bid_240)
EE_ENTRY_LO  = 0.82   # faixa de entrada: mínimo
EE_ENTRY_HI  = 0.86   # faixa de entrada: máximo
EE_HEDGE_THR = 0.50   # crossing que ativa hedge (compra lado oposto)
EE_MAX_SECS  = 180    # janela máxima para entrada EE
```

### 8.2 Classes adicionadas

**`_EarlyLeaderTracker`** — acumula snaps por janela e computa EL + F3 + el_vel:
- `_s240`: snaps coletados em secs 181–240 (janela de detecção)
- `_s180`: snaps coletados em secs 121–180 (janela de continuidade F3)
- `early_leader`: `"UP"` | `"DOWN"` | `None`
- `el_bid_240`: bid médio do EL na janela 240
- `el_bid_180`: bid médio do EL na janela 180
- `el_vel`: `el_bid_180 − el_bid_240` (velocidade de crescimento)
- `cont_ok`: `True` se min(bids na janela 180) >= EE_CONT_MIN
- `signal_ok` (property): `early_leader AND cont_ok AND el_vel >= EE_VEL_MIN`

**`_EEPosition`** — rastreia estado da posição EE paper:
- Estados: `none` → `entry` → `hedged` → `closed`
- `qty = 3.0` (metade do padrão para controle de risco)
- `_just_closed`: flag que garante o outcome seja logado apenas no snap de transição

### 8.3 Lógica de entrada e hedge

```
1. EL tracker acumula snaps continuamente para cada slug
2. Quando signal_ok=True E secs 30–180 E EL bid em 0.82–0.86 E sem posição aberta → abre entrada
3. Se em posição "entry" e EL bid cruza < 0.50 → ativa hedge (compra lado oposto)
4. Resolução (secs <= 35):
   - WIN: EL bid >= 0.85
   - WIN_BOUNCE: hedge ativo, EL venceu (EL bid >= 0.85)
   - WIN_HEDGE: hedge ativo, lado oposto venceu
   - REVERSAL: EL bid <= 0.05 sem hedge
```

### 8.4 Campos adicionados ao log (type=monitor_snap)

```
el_leader      — "UP" | "DOWN" | null
el_bid_240     — bid médio do EL na janela 240
el_bid_180     — bid médio do EL na janela 180
el_vel         — crescimento do bid EL
el_cont_ok     — True se F3 satisfeita
sig_ee         — True no snap onde o sinal EE disparou
ee_state       — estado atual da posição ("none"|"entry"|"hedged"|"closed")
ee_side        — lado comprado ("UP"|"DOWN"|null)
ee_entry_ep    — preço de entrada
ee_hedge_ep    — preço do hedge (0 se não hedgeou)
ee_outcome     — resultado ("WIN"|"WIN_BOUNCE"|"WIN_HEDGE"|"REVERSAL"|"") — só no snap de fechamento
ee_pnl         — PnL do trade — só no snap de fechamento
```

**Nota importante:** `ee_outcome` e `ee_pnl` são preenchidos apenas no snap de transição para "closed" (flag `_just_closed`). Nos snaps seguintes ficam vazios. Isso evita duplicação nas análises.

### 8.5 Bug corrigido em 2026-05-21

**Bug:** `ee_outcome` era escrito em todos os snaps após o fechamento do trade, gerando 10–15 linhas por trade e inflando contagens brutas.  
**Correção:** adicionado `_just_closed: bool` em `_EEPosition`. O flag é `True` apenas no snap do fechamento e é zerado imediatamente após o logging.

---

## 9. Resultados ao Vivo (runner de teste)

Log: `market_monitor_20260521_085611.jsonl` (08:56–10:30)  
Obs: bug de logging ainda presente nesse arquivo (corrigido no log seguinte).

| # | Slug | Lado | Entry | Outcome | PnL (qty=3) |
|---|---|---|---|---|---|
| 1 | `…1368400` | UP | 0.86 | WIN | +$0,42 |
| 2 | `…1369000` | DOWN | 0.83 | WIN | +$0,51 |

**Total: 2 trades, 2 WIN, PnL = +$0,93**

Log atual (bug corrigido): `market_monitor_20260521_103036.jsonl` (10:30+)

---

## 10. Testes Pendentes (executar nos logs do runner real)

> **No PC do runner real:** `git pull` e então executar os scripts abaixo.

### TESTE A — EL nos logs reais
```bash
python analyze_early_leader_all_logs.py --log-dir logs/
```
Verificar: EL tem 79%+ baseline e 94% com F3? Proporção de inversões similar?

### TESTE B — EXC.OPOSTO nos dados reais
```bash
python analyze_monitor_pnl.py --all-logs
```
Verificar: filtro EXC.OPOSTO (bloquear AR quando EL contradiz o lado) melhora PnL nos logs reais?

### TESTE C — Gate range_15s nos logs reais
```bash
python analyze_entry_threshold_and_gaps.py --drop-min 0.08
```
Verificar: qual % das perdas tem `range_15s >= 0.03`? Qual custo (trades bloqueados)?

### TESTE D — EL stable + sem stop (script pendente)
Criar `analyze_el_hold_no_stop.py`.  
Hipótese: EL estável em secs 200, bid >= 0.82, hold to resolution sem stop → ~90% WR.

### TESTE E — Inversão de EL como sinal de entrada (script pendente)
Criar `analyze_el_flip_signal.py`.  
Hipótese: EL inverte em secs 180–121 com bid 0.60–0.72 → comprar novo líder, stop 3T, hold.

---

## 11. Roadmap

```
FASE 1 — Análise (concluída):
  [x] analyze_early_leader_all_logs.py      — EL predictivity em 661 slugs
  [x] analyze_entry_threshold_and_gaps.py   — threshold + book gaps
  [x] analyze_monitor_pnl.py --all-logs     — EXC.OPOSTO identificado

FASE 2 — Simulação (concluída):
  [x] _sim_early_entry.py                   — EE com/sem stop, sem lookahead bias
  [x] _sim_early_entry_v2.py                — hedge em 0.50, filtros el_vel / spot / secs

FASE 3 — Runner de teste (em andamento):
  [x] EE implementado em run_market_monitor.py
  [x] Bug de logging corrigido (_just_closed)
  [ ] Coletar 24h+ de dados com EE ativo
  [ ] Analisar: python analyze_monitor_pnl.py --all-logs (novo log com campos EE)

FASE 4 — Validação e produção (futuro):
  [ ] Comparar WR/PnL real vs simulação (~94% WR, ~$0.61/trade)
  [ ] Implementar EXC.OPOSTO no runner real (bloquear AR contraditório)
  [ ] Implementar gate range_15s se validado nos logs reais
  [ ] Avaliar EL flip signal (comprar reversão)
```

---

## 12. Parâmetros de Referência

| Parâmetro | Valor atual | Observação |
|---|---|---|
| `EE_EL_MIN` | 0.55 | Threshold de detecção do EL |
| `EE_CONT_MIN` | 0.70 | F3: mínimo do bid EL em secs 180–121 |
| `EE_VEL_MIN` | 0.08 | Velocidade mínima de crescimento do bid EL |
| `EE_ENTRY_LO` | 0.82 | Faixa de entrada: mínimo |
| `EE_ENTRY_HI` | 0.86 | Faixa de entrada: máximo |
| `EE_HEDGE_THR` | 0.50 | Crossing que ativa hedge |
| `EE_MAX_SECS` | 180 | Janela máxima de entrada |
| `qty EE` | 3 shares | Metade do padrão; ajustar após validação |
| `qty AR normal` | 6 shares | Não alterar sem validação |
| `entry_min AR` | 0.88 | Não alterar sem validação |
| `stop AR` | 2T | Para EE, sem stop (hold to resolution) |
| `range_15s gate` | 0.03 | Proposto; validar nos logs reais (Teste C) |
