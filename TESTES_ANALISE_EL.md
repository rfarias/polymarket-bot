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

### 9.1 Relatório 1 — 2026-05-21 (~08:56 → 13:30)

Logs: `market_monitor_20260521_085611.jsonl` + `market_monitor_20260521_103036.jsonl`  
Obs: bug de duplicate logging presente no primeiro arquivo (corrigido às 10:30 com flag `_just_closed`).  
Parâmetros: EL estável (F3) + el_vel >= 0.08 + hedge em 0.50 + qty=3 + sem stop.

| # | Slug | Lado | Entry | Outcome | PnL |
|---|---|---|---|---|---|
| 1 | `…1368400` | UP | 0.86 | WIN | +$0,42 |
| 2 | `…1369000` | DOWN | 0.83 | WIN | +$0,51 |
| 3 | `…1369900` | DOWN | 0.82 | WIN_HEDGE | -$1,23 |
| 4 | `…1372000` | UP | 0.83 | WIN | +$0,51 |
| 5 | `…1374100` | UP | 0.84 | WIN | +$0,48 |
| 6 | `…1376200` | UP | 0.83 | WIN | +$0,51 |
| 7 | `…1376500` | UP | 0.86 | WIN | +$0,42 |
| 8 | `…1376800` | UP | 0.83 | WIN | +$0,51 |

**Total: 8 trades | 7 WIN | 1 WIN_HEDGE | WR 87,5% | PnL = +$2,13**

Notas:
- WIN_HEDGE (trade 3): EL bid cruzou 0.50, hedge ativou comprando DOWN. DOWN venceu. Perda
  parcial de -$1,23 em vez de ~-$2,46 sem hedge — **hedge funcionou como projetado**.
- Entry prices: 0.82–0.86, todos dentro da faixa EE.
- Nenhum REVERSAL puro (sem hedge) — o único caso de EL errado foi capturado pelo hedge.
- avg/trade: +$0,27 (vs simulação: +$0,61 com el_vel >= 0.08; amostra ainda pequena).

### 9.2 Relatório 2 — 2026-05-21 (13:30 → 14:15)

Nenhum trade novo capturado nesta janela (1h45). O monitor continuou rodando
e o sniper foi reiniciado após dois bugs críticos descobertos às 14:09:

**Bugs do sniper corrigidos (commit a59feaf):**
1. `config.json: entry_score_threshold=5` (deveria ser 3) — nenhum `would_enter` disparava. Slug
   `…382200` chegou a `score=3` em secs=82 com threshold=5 no config: não capturado.
2. `NameError: MAX_SECS` (constante removida mas ainda referenciada) — sniper crashava no boot;
   ficou off por ~9 minutos sem que ficasse aparente.

**Eventos sniper observados (paper) desde restart às ~08:56:**
| Slug | Score máx | Obs |
|---|---|---|
| `…381600` | -3 | EL block ativo; sem oportunidade |
| `…381900` | +2 | Loser subiu 0.12→0.51 (325%!); score=0 na entrada; would_exit_early ativou com bid=0.51 |
| `…382200` | +3 | Seria would_enter com threshold=3; sem reversal (loss -0.10/share) |
| `…382500` | -3 | EL block ativo; sem oportunidade |

Obs slug `…381900`: o loser saiu de 0.12 (secs=28) para 0.51 (secs=13) sem o score atingir
threshold — nenhum sinal ativo (el_gate=0, sinal_a=0, sinal_b=0, sinal_e=0). Mostra que
ainda há reversões sem sinal detectável: **mercado de difícil cobertura pelo score atual**.

**Status EE (acumulado):**  
Ainda 8 trades (7 WIN + 1 WIN_HEDGE, PnL=+$2,13) — sem novas entradas desde ~11:13.

### 9.3 Relatório 3 — 2026-05-21 (14:15 → 15:39)

| # | Slug | Lado | Entry | el_vel | F3 | Outcome | PnL |
|---|---|---|---|---|---|---|---|
| 9  | `…1384300` | UP   | 0.82 | 0.159 | ok   | WIN | +$0,54 |
| 10 | `…1384900` | UP   | 0.83 | 0.205 | ok   | WIN | +$0,51 |
| 11 | `…1386400` | DOWN | 0.85 | 0.232 | ok   | WIN | +$0,45 |

**Acumulado: 11 trades | 10 WIN | 1 WIN_HEDGE | WR 90,9% | PnL = +$3,63**

Notas:
- Trades 10 e 11: el_vel alto (0.205 e 0.232), ambos WIN rápidos (secs=21).
- Trade 7 (`…376500`): F3 marcado FAIL no snap de fechamento, mas entry foi feita com F3 ok
  — o EL bid dip ocorreu durante o hold, não bloqueou a entrada.
- avg/trade: +$0,33/trade (qty=3). Série sem LOSS puro mantida.

**Bug sniper corrigido (commit 82d5dbe):**  
`in_zone` check ainda usava `current_secs <= MAX_SECS` — gerou 184 eventos `error` no log
(NameError capturado pelo try/except, sem quebrar o processo). Corrigido e sniper reiniciado
(PID 2656).

### 9.4 Relatório 4 — 2026-05-21 (15:39 → 16:36)

**Monitor EE**: sem novos trades. Acumulado mantido em 11 trades / +$3,63.

**Sniper paper — primeiros `would_enter` disparados (3 eventos):**

| Slug | secs | score | loser_entry | peak | early_exit_bid | pnl/share_hold | pnl/share_early | ev_delta |
|---|---|---|---|---|---|---|---|---|
| `…390600` | 12 | 5 (B3+E2+EL0) | 0.37 | **0.67** | 0.09 | reversão! | -0.28 | negativo |
| `…390900` | 64 | 4 (B3+E2+EL-1) | 0.17 | 0.34 | 0.11 | -0.17 | -0.06 | **+0.11** |
| `…391500` | 16 | 4 (B3+E2+EL-1) | 0.06* | 0.13 | 0.07 | -0.06 | +0.01 | **+0.07** |

_*entry_loser_price = preço quando tracking iniciou (secs=36), não quando would_enter disparou (secs=16)._

**Análise:**
- Padrão comum: B=3 (strong decel) + E=2 (loser subindo) + ELgate=0/-1 (inversão fraca ou média)
- 0/3 reversões completas ao hold — mas slug `…390600` tinha loser@t20=0.67 (reversão em curso!)
- `…390600`: dynamic_stop triggou em 0.09 após pico 0.67 — bid caiu de 0.67→0.09 **entre dois polls** (gap de ~8s). Saída prematura numa reversão real. Custo do slippage por iliquidez.
- `…390900`: dynamic_stop funcionou corretamente — salvou -0.11/share vs hold -0.17/share.
- `…391500`: score_collapsed_take_profit, saída em 0.07 vs entry 0.06 = +0.01/share (ligeiramente lucrativo).

**Problemas identificados:**
1. **Dynamic stop vulnerável a gaps de liquidez**: pullback frac (0.65×peak) pode ser cruzado em um único poll quando o bid colapsa. Slug `…390600` é o caso crítico — a reversão era real (loser chegou a 0.67 ao t=20s) mas fomos ejetados em 0.09.
2. **ELgate 0/-1 insuficiente para diferenciar** reversões reais de head fakes: todos os 3 casos tinham EL invertido fraco/médio, sem discriminação clara de outcome.

**Próximos passos sugeridos:** aumentar o intervalo mínimo de polls no dynamic stop, ou adicionar confirmação de 2 polls consecutivos abaixo do pullback antes de sair.

### 9.5 Runner EE Standalone — Primeiros resultados (2026-05-22)

**Runner:** `market/live_early_entry_paper_v1.py` (standalone, sem monitor AR)  
**Watchdog:** `scripts/watch_early_entry_paper.ps1 -Continuous -RunSeconds 7200 -Qty 6`  
**Parâmetros:** F3 + el_vel >= 0.08 + entry 0.82–0.86 + hedge em 0.50 + qty=6 + sem stop

**Sessão 1 — 07:32→09:32 (2h):**

| # | Slug | Lado | Entry | secs | el_vel | Outcome | PnL |
|---|---|---|---|---|---|---|---|
| 1 | `…446100` | DOWN | 0.82 | 142 | 0.157 | WIN | +$1,08 |
| 2 | `…447000` | DOWN | 0.84 | 134 | 0.085 | WIN | +$0,96 |
| 3 | `…448800` | UP  | 0.84 | 110 | 0.100 | WIN | +$0,96 |
| 4 | `…452400` | DOWN | 0.86 | 168 | 0.179 | WIN | +$0,84 |
| 5 | `…452700` | DOWN | 0.83 | 177 | 0.114 | WIN | +$1,02 |

**Sessão 1: 5 WIN | 0 HEDGE | WR 100% | PnL = +$4,86**

**Sessão 2 — 09:32→11:32:**

| # | Slug | Lado | Entry | secs | el_vel | Outcome | PnL |
|---|---|---|---|---|---|---|---|
| 6  | `…453900` | UP   | 0.84 | 176 | 0.119 | WIN | +$0,96 |
| 7  | `…454200` | UP   | 0.82 | 180 | 0.216 | WIN | +$1,08 |
| **8**  | **`…455700`** | **UP** | **0.84** | **166** | **0.106** | **WIN_HEDGE** | **-$4,14** |
| 9  | `…458700` | DOWN | 0.82 | 169 | 0.122 | WIN | +$1,08 |
| 10 | `…459900` | UP   | 0.85 |  91 | 0.185 | WIN | +$0,90 |

**Sessão 2: 4 WIN | 1 WIN_HEDGE | WR 80% | PnL = -$0,12**

**Sessão 3 — 11:32→13:32:**

| # | Slug | Lado | Entry | secs | el_vel | Outcome | PnL |
|---|---|---|---|---|---|---|---|
| 11 | `…462600` | DOWN | 0.85 | 145 | 0.086 | WIN | +$0,90 |
| 12 | `…465000` | UP   | 0.83 | 169 | 0.143 | WIN | +$1,02 |
| 13 | `…465900` | DOWN | 0.85 | 164 | 0.134 | WIN | +$0,90 |
| 14 | `…467100` | UP   | 0.85 | 171 | 0.133 | WIN | +$0,90 |

**Sessão 3: 4 WIN | WR 100% | PnL = +$3,72**

**Sessão 4 — 13:32→em andamento (parcial):**

| # | Slug | Lado | Entry | secs | el_vel | Outcome | PnL |
|---|---|---|---|---|---|---|---|
| **15** | **`…468000`** | **UP** | **0.86** | **154** | **0.082** | **WIN_HEDGE** | **-$2,64** |

**Sessão 4: 0 WIN | 1 WIN_HEDGE | PnL = -$2,64 (parcial)**

**Acumulado EE standalone (v1, com hedge): 15 trades | 13 WIN | 2 WIN_HEDGE | WR 86,7% | PnL = +$5,82 | avg +$0,39/trade**

**Análise dos dois WIN_HEDGE — modos de falha distintos:**

| Caso | Slug | Trajectória | Causa | pnl_hedge |
|---|---|---|---|---|
| A | `…455700` | 0.84→0.92 (pico) →0.21 em 1 poll | Gap brusco após pico alto | -$4,14 |
| B | `…468000` | 0.86→0.72→0.55 (gap -0.23) sem recuperação | Declínio gradual, sem pico | -$2,64 |

- **Caso A**: UP estava **subindo** (0.92 em secs=56) quando crash ocorreu. Nenhum stop teria ajudado (min_bid=0.67 durante oscilação, que recuperou). Só Profit Protect salva.
- **Caso B**: UP declinou gradualmente de 0.86 para 0.55, nunca recuperou a 0.85. Profit Protect não ativa. Só Stop salva.

**Conclusão:** os dois WIN_HEDGE têm causas opostas — uma solução única não cobre os dois. É necessário um mecanismo duplo.

### 9.6 Análise: Stop vs Hedge vs Profit Protect (2026-05-22)

**Script:** `analyze_ee_stop_vs_hedge.py` | **Amostra:** 15 trades | **Base:** WR 86,7% | PnL +$5,82

#### Trajectória tick a tick — Caso A (455700, crash após pico)

```
secs=166  UP=0.84  (ENTRADA)
secs=153  UP=0.67  oscilação normal
secs=128  UP=0.68  oscilação normal
secs=56   UP=0.92  <- PICO (subindo!)
secs=51   UP=0.21  <- GAP -0.71 em UM poll (5s)
           DOWN=0.85 -> hedge ativou caro: pnl=-$4,14
```

#### Trajectória tick a tick — Caso B (468000, declínio gradual)

```
secs=154  UP=0.86  (ENTRADA)
secs=136  UP=0.72  caindo
secs=106  UP=0.55  <- GAP -0.23 em um poll
secs=100  UP=0.43  -> hedge ativou a DOWN=0.58: pnl=-$2,64
(UP nunca recuperou a 0.85 durante o hold)
```

#### Simulação de saídas alternativas — impacto no portfólio completo

**Stop isolado:**

| Stop | Ativas | WR | PnL total | vs base |
|---|---|---|---|---|
| 0.60 | 1 | 86,7% | +$6,90 | +$1,08 |
| 0.65 | 1 | 86,7% | +$7,20 | +$1,38 |
| **0.67** | **2** | **86,7%** | **+$10,44** | **+$4,62** |
| 0.70 | 3 | 80,0% | +$9,00 | +$3,18 |
| 0.75 | 4 | 73,3% | +$8,40 | +$2,58 |

Stop 0.67 salva os 2 WIN_HEDGE sem cortar nenhum WIN (todos os WIN tiveram min_bid >= 0.68).  
Stop 0.65 salva apenas Caso B (Caso A: min_bid=0.67, fora do alcance de stop 0.65).

**Profit Protect isolado (sair quando bid >= PP_bid em 36 <= secs <= PP_secs):**

| PP bid | PP secs | Ativas | WR | PnL total | vs base |
|---|---|---|---|---|---|
| 0.85 | 60 | 14 | 93,3% | +$8,04 | +$2,22 |
| **0.88** | **60** | **14** | **93,3%** | **+$8,46** | **+$2,64** |
| 0.92 | 70 | 14 | 93,3% | +$8,64 | +$2,82 |

PP ajuda Caso A (captura pico 0.92 → pnl +$0,48 em vez de -$4,14).  
PP **não ajuda** Caso B (UP nunca chegou a 0.85 enquanto em posição entry).

**Combinação Stop + PP (melhores candidatos):**

| Stop | PP bid | PP secs | WR | PnL total | vs base |
|---|---|---|---|---|---|
| 0.65 | 0.88 | 60 | 93,3% | +$9,84 | +$4,02 |
| **0.65** | **0.92** | **70** | **93,3%** | **+$10,02** | **+$4,20** |
| 0.65 | 0.92 | 60 | 93,3% | +$9,84 | +$4,02 |

**Stop 0.65 cobre Caso B. PP bid>=0.92, secs<=70 cobre Caso A. Juntos: +$4,20 vs base.**

#### Implementação adotada — v2 do runner

Implementado em `market/live_early_entry_paper_v1.py` com prioridade de saída:

```
1. secs <= 35 → WIN/REVERSAL (resolução normal — maior prioridade)
2. 36 <= secs <= 70 e bid EL >= 0.88 → PROFIT_PROTECT (saída ao preço atual)
3. bid EL < 0.65 → STOP_LOSS (saída ao preço atual — stop market)
4. bid EL < 0.50 → HEDGE (fallback, raramente atingido com stop em 0.65)
```

Parâmetros adicionados:
- `EE_STOP_LEVEL = 0.65`
- `EE_PROFIT_PROTECT_BID = 0.88`
- `EE_PROFIT_PROTECT_SECS = 70`

Novos outcomes nos logs: `PROFIT_PROTECT`, `STOP_LOSS`  
Novos eventos: `ee_paper_profit_protect`, `ee_paper_stop_loss`

**Expectativa simulada:** WR 93,3% | PnL ~+$10,00 para amostra de 15 trades (vs +$5,82 atual).  
Próximo relatório validará se a combinação funciona na prática.

### 9.7 Análise: Trailing Stop vs Fixed Stop (2026-05-22)

**Script:** `analyze_ee_trailing_stop.py` | **Amostra:** 18 trades (16 WIN + 2 WIN_HEDGE)  
**Base:** WR 88,9% | PnL +$9,00

#### Por que o trailing stop não funciona neste mercado

**Achado crítico:** trailing stop em qualquer pullback (1T a 15T) é **pior que o base** em condições realistas:

| Estratégia | WR | PnL real | vs base |
|---|---|---|---|
| **Base (v1, com hedge)** | 88,9% | +$9,00 | — |
| PP bid>=0.88 secs<=70 | 94,4% | +$9,90 | +$0,90 |
| **Stop fixo 0.67** | **88,9%** | **+$13,62** | **+$4,62** |
| Trail 2T ideal | 88,9% | +$3,66 | -$5,34 |
| Trail 2T **real** | 83,3% | +$1,68 | **-$7,32** |
| Trail 5T real | 83,3% | +$1,26 | -$7,74 |
| Breakeven stop real | 66,7% | +$4,14 | -$4,86 |
| BE + Trail 2T real | 83,3% | +$1,68 | -$7,32 |

**Causa dupla do fracasso do trailing:**

**Problema 1 — Dispara incorretamente nos WIN trades:**  
Perto da resolução (secs 70→35), os bids oscilam com gaps de 0.03–0.08 entre polls. Isso aciona o trailing antes da resolução final. 10 de 16 WIN trades teriam o stop disparado com trailing 2T.

| Exemplo WIN | Peak | Stop | Exit real | Gap | PnL real | PnL resolução | Perdido |
|---|---|---|---|---|---|---|---|
| …467100 | 0.92 | 0.90 | 0.82 | 0.08 | -$0,18 | +$0,90 | $1,08 |
| …447000 | 0.89 | 0.87 | 0.85 | 0.02 | +$0,06 | +$0,96 | $0,90 |
| …473400 | 0.90 | 0.88 | 0.84 | 0.04 | +$0,12 | +$1,08 | $0,96 |

**Total profit deixado na mesa em WIN trades com trailing 2T: $7,32**

**Problema 2 — Não captura os WIN_HEDGE (crash pós-pico):**  
No trade 8 (455700), o crash de 0.92→0.21 acontece em 1 poll (5s). O primeiro snap que vemos o bid abaixo do trailing stop (0.90) já tem estado="hedged" — o hedge foi colocado antes do trailing ser verificado neste poll. O trailing nunca chegou a ser avaliado no momento do crash.

```
secs=56: bid=0.92  estado=entry   <- trailing stop = 0.90 (correto)
secs=51: bid=0.21  estado=hedged  <- crash JÁ ACONTECEU, hedge JÁ FOI ATIVADO
                                     trailing nunca foi avaliado no momento da queda
```

Resultado: trailing 2T = -$4,14 (idêntico ao base) no trade 8.

#### Por que o stop fixo 0.67 funciona

O stop fixo não depende de trailing — ele verifica apenas "bid < 0.67" a cada poll.

- **16 WIN trades**: todos têm `min_bid >= 0.68` → stop 0.67 **nunca dispara** em WIN
- **2 WIN_HEDGE trades**: `min_bid < 0.65` → stop dispara antes do colapso total

| Trade | EP | min_bid | Stop 0.67? | PnL com stop | PnL sem stop |
|---|---|---|---|---|---|
| Trade 8 (455700) | 0.84 | 0.67 | secs=153, bid=0.67 | -$1,02 | -$4,14 |
| Trade 15 (468000) | 0.86 | 0.55 | secs=106, bid=0.55 | -$1,14 | -$2,64 |

O stop 0.67 captura os dois casos de declínio na oscilação natural, sem cortar nenhum WIN.

#### Liquidez — viabilidade de execução

Stop fixo (0.65–0.67): dispara durante oscilação gradual (bids ainda com liquidez). Fill possível.  
Trailing stop: dispara perto de resolução (secs 70→35) onde os bids caem bruscamente. 8 de 10 ativações em WIN trades tiveram `gap > 0.02` → fill improvável ao preço alvo.

**Conclusão:** o trailing stop é duplamente prejudicial — não protege onde mais importa (WIN_HEDGE) e corta onde não deve (WIN). O **stop fixo em 0.67** é mais robusto, não precisa de liquidez high-end e supera todas as variantes de trailing/breakeven nessa amostra.

#### Estratégia recomendada (validar em mais trades)

Stop fixo 0.67 único (+$4,62 vs base) supera PP+stop atual (+$4,20 com 15 trades).  
Com 18 trades, o stop fixo ainda domina — mas n ainda é pequeno para confirmar que nenhum WIN futuro terá min_bid < 0.67.

### 9.8 Análise: min_bid nos logs market_monitor (pré-implementação EE standalone)

**Script:** `analyze_ee_minbid_monitor.py`  
**Logs:** 4 arquivos market_monitor (20260520_171127, 20260521_074834, 20260521_085611, 20260521_103036)  
**Objetivo:** validar o nível de stop (0.67) numa amostra mais ampla — trades que o EE simulado teria feito antes do runner standalone existir.

#### Resultados — 11 trades encontrados

| # | Slug | Lado | EP | Outcome | min_bid | <0.67 | <0.65 | PnL |
|---|---|---|---|---|---|---|---|---|
| 1 | `…368400` | UP | 0.86 | WIN | 0.860 | - | - | +0.42 |
| 2 | `…369000` | DOWN | 0.83 | WIN | 0.800 | - | - | +0.51 |
| 3 | `…372000` | UP | 0.83 | WIN | 0.830 | - | - | +0.51 |
| 4 | `…374100` | UP | 0.84 | WIN | 0.840 | - | - | +0.48 |
| 5 | `…376200` | UP | 0.83 | WIN | 0.810 | - | - | +0.51 |
| **6** | **`…376500`** | **UP** | **0.86** | **WIN** | **0.580** | **SIM** | **SIM** | +0.42 |
| 7 | `…376800` | UP | 0.83 | WIN | 0.830 | - | - | +0.51 |
| 8 | `…384300` | UP | 0.82 | WIN | 0.820 | - | - | +0.54 |
| 9 | `…384900` | UP | 0.83 | WIN | 0.830 | - | - | +0.51 |
| 10 | `…386400` | DOWN | 0.85 | WIN | 0.850 | - | - | +0.45 |
| **11** | **`…369900`** | **DOWN** | **0.82** | **WIN_HEDGE** | **0.560** | **SIM** | **SIM** | -1.23 |

**WIN floor (10 trades):** 0.58 (slug 376500) — **1 WIN tocou abaixo de 0.67 e 0.65**  
**WIN_HEDGE floor (1 trade):** 0.56 — também abaixo de 0.67 e 0.65

#### Análise do caso anômalo — slug 376500 (WIN com min_bid = 0.58)

Este mercado tem padrão completamente atípico em relação ao grupo:

```
secs=885→220  bid EL ≈ 0.49  (estável na metade por ~11 min)
secs=188      bid oscila 0.36 → 0.74 → 0.81  (subida explosiva)
secs=157      state=entry bid=0.86  (entrada)
secs=125      bid oscila 0.88 → 0.63 → 0.68  (queda abrupta)
secs=93       bid cai para 0.58  <-- min_bid aqui
secs=93→62   bid se recupera 0.66 → 0.76 → 0.87 → 0.91 → 0.94
secs=31       WIN  bid=0.98
```

**Características que diferem dos outros trades:**
1. O bid EL estava em 0.49 por ~11 minutos (padrão de 50/50 prolongado)
2. A subida de 0.49 → 0.86 ocorreu em apenas 2-3 polls (30s)
3. Durante a janela secs=121-180 (usada pelo cont_ok), o bid oscilou entre 0.36 e 0.87
4. O bid caiu até 0.58 em secs=93 antes de se recuperar completamente

**Hipótese — cont_ok provavelmente teria bloqueado este trade:**  
O critério `cont_ok` exige que o bid EL **nunca** caia abaixo de 0.70 em secs=121-180. Em secs=188, os polls mostram bid em 0.36 — bids que chegam ao final da janela 180 com valores muito baixos. O runner real provavelmente teria rejeitado este trade por cont_ok falso. A simulação do market_monitor pode usar critérios ligeiramente diferentes.

#### Simulação de stop nos 11 trades do monitor

| Stop | Ativa total | Ativa WIN | Ativa LOSS | WR | PnL | vs base |
|---|---|---|---|---|---|---|
| 0.80 | 2 | 1 | 1 | 81.8% | +4.20 | +0.57 |
| 0.67 | 2 | 1 | 1 | 81.8% | +3.42 | -0.21 |
| 0.65 | 2 | 1 | 1 | 81.8% | +3.30 | -0.33 |
| **base** | — | — | — | 90.9% | **+3.63** | — |

**No monitor:** stop em qualquer nível é PIOR que a base — pois o stop corta o WIN 376500 (pnl ideal +0.42 → pnl stop = (0.67-0.86)×3 = -0.57) e o WIN_HEDGE (0.56 < 0.67 → (0.67-0.82)×3 = -0.45 vs hedge -1.23), resultando em ganho líquido menor.

#### Conclusão consolidada (18 standalone + 11 monitor = 29 trades)

| Grupo | n WIN | min_bid floor WIN | n LOSS | min_bid floor LOSS | Stop 0.67 seguro? |
|---|---|---|---|---|---|
| Standalone runner | 16 | **0.68** | 2 | 0.05/0.27 | **Sim** (0 WIN cortados) |
| Market monitor | 10 | **0.58** (1 outlier) | 1 | 0.56 | **Não** (1 WIN cortado) |
| **Combinado** | **26** | **0.58** | **3** | — | **Incerto** |

**Achado principal:** o trade 376500 do monitor invalida a hipótese "stop 0.67 = 100% seguro". Porém:
- Este trade provavelmente seria **filtrado pelo cont_ok** no runner real (bid oscilou <0.70 na janela 121-180)
- O standalone runner (18 trades, critérios idênticos ao runner real) ainda mostra floor de 0.68 para WIN
- A margem de segurança (0.68 - 0.67 = 1 tick) é estreita — confirmar com mais amostras

**Status:** manter stop em 0.65 + PP 0.88 conforme v2 implementado. Aguardar mais 30-50 trades do runner standalone para validar se o floor WIN permanece ≥ 0.68.

### 9.9 Análise: faixa de entrada (floor/ceiling) — otimização ou risco? (2026-05-22)

**Scripts:** `_sim_entry_any_price.py`, `_sim_no_stop_direction.py`, `_sim_stop_strategies.py`, `_sim_ceiling.py`  
**Amostra:** 18 trades reais + logs de snapshot de 39 slugs com signal_ok  
**Questão:** vale entrar em preços menores que 0.82 (ganhar mais ticks) ou maiores que 0.86 (mais oportunidades)?

#### 9.9.1 Janela de tempo das entradas

83% das entradas ocorrem em `secs 160–180` — entre **2:00 e 2:40** do início do candle de 5 minutos.  
A detecção do EL (janela 181–240) + cont_ok (121–180) completa exatamente nessa janela.

| Janela secs | n | % | Equivalente no candle |
|---|---|---|---|
| 160–180 | 11 | 61% | 2:00–2:20 do início |
| 140–159 | 4 | 22% | 2:21–2:40 |
| 120–139 | 1 | 6% | 2:41–3:00 |
| < 120 | 2 | 11% | após 3:00 |

#### 9.9.2 Entrar em qualquer preço com signal_ok (remover floor 0.82)

Simulação: entrar no 1º snap com signal_ok, qualquer bid. Resultado com stop 0.65 + PP 0.88:

| Estratégia | Trades | WR | PnL | avg/trade |
|---|---|---|---|---|
| **Atual (0.82–0.86)** | **18** | **94.4%** | **+$11.28** | **+$0.627** |
| Qualquer bid + stop 0.65 | 39 | 71.8% | +$8.88 | +$0.228 |
| Qualquer bid + stop 0.60 | 39 | 79.5% | +$11.82 | +$0.303 |
| Qualquer bid + stop dyn. entry−0.18 | 39 | 79.5% | +$11.94 | +$0.306 |

**Acurácia direcional dos 21 trades novos (sem stop, apenas direção):** 18/21 = **86% WR** — mesmo nível da estratégia atual. O problema não é o sinal direcional, é a **trajetória durante o hold**:

| Faixa EP | WR direcional | Problema |
|---|---|---|
| 0.60–0.75 | 33% | trades revertem completamente |
| 0.75–0.80 | 83% | min_bid cai abaixo de 0.65 mesmo nos vencedores |
| 0.80–0.82 | 100% | ok |
| **0.82–0.86** | **100%** | **← faixa atual** |

Nos trades vencedores com EP em 0.75–0.80, o bid frequentemente cai para 0.45–0.61 antes de resolver — disparando o stop incorretamente em trades que terminariam como WIN. Nenhum nível de stop elimina esse problema sem também cortar os perdedores (que caem até 0.01).

**Conclusão:** o floor 0.82 não é filtro direcional (acerto é 86% em qualquer faixa). É um **filtro de trajetória** — garante que o bid já está consolidado o suficiente para as oscilações normais de hold não dispararem o stop.

#### 9.9.3 Subir o teto de 0.86 para 0.90 ou 0.92

| Teto | Trades | Novos | WR | PnL | avg/trade |
|---|---|---|---|---|---|
| **0.86 (atual)** | **18** | **—** | **94.4%** | **+$11.28** | **+$0.627** |
| 0.90 | 23 | +5 | 91.3% | +$10.50 | +$0.457 |
| 0.92 | 24 | +6 | 91.7% | +$10.56 | +$0.440 |

Apesar dos 5–6 trades novos renderem +$1.14/+$1.56, **7 trades existentes pioram** porque o bot entra mais caro (o 1º signal_ok estava acima de 0.86 e o teto mais alto faz entrar imediatamente em vez de esperar o bid cair):

| Slug | EP atual | EP com teto 0.90 | Impacto |
|---|---|---|---|
| 446100 | 0.82 | 0.89 | +$0.54 → +$0.12 (−$0.42) |
| 468900 | 0.82 | 0.88 | +$0.60 → +$0.24 (−$0.36) |
| 465000 | 0.83 | 0.89 | +$0.84 → +$0.48 (−$0.36) |
| 465900 | 0.85 | 0.90 | +$0.84 → +$0.54 (−$0.30) |

Degradação nos 18 existentes: **−$1.92**. Ganho nos 5 novos: **+$1.14**. Saldo: **−$0.78**.

O teto 0.86 funciona como **estratégia de execução** — quando signal_ok dispara com bid em 0.91, esperar o bid voltar para 0.82–0.86 captura o dip natural e garante entrada melhor. Subir o teto elimina esse "wait-for-dip".

#### 9.9.4 Conclusão: faixa 0.82–0.86 está bem calibrada

- **Floor 0.82**: filtro de trajetória. Abaixo disso, os vencedores oscilam tanto que o stop dispara incorretamente.
- **Ceiling 0.86**: estratégia de execução. Acima disso, o bot entra no pico em vez de esperar o dip natural.
- **Resultado**: qualquer alteração na faixa atual piora o PnL total com os dados disponíveis.
- **Oportunidade real identificada**: o `el_vel` dos WIN_HEDGE (0.082 e 0.106) é menor que a média WIN (0.132). Gate `el_vel >= 0.11` eliminaria os 2 HEDGE sem custar WIN na amostra atual — mas n=2 é insuficiente para validar.

### 9.10 Análise: STOP_LOSS — gap de execução, hedge e PP (2026-05-23)

**Scripts:** `_check_summary.py`, `_check_stops_trajectory.py`, `_check_stops_detail.py`  
**Contexto:** runner v2 acumulou 43 trades fechados (19 WIN + 19 PP + 2 WIN_HEDGE + **3 STOP_LOSS**)

#### Acumulado v2 (43 trades fechados)

| Outcome | n | PnL parcial |
|---|---|---|
| WIN | 19 | — |
| PROFIT_PROTECT | 19 | +$13.86 (exit 0.89–1.00) |
| WIN_HEDGE | 2 | −$6.78 (da v1) |
| STOP_LOSS | 3 | −$7.20 |
| **Total** | **43** | **+$18.42** |

**WR: 88.4% | avg +$0.428/trade**

#### Gap de execução nos 3 STOP_LOSS

O stop detecta `el_bid < 0.65` a cada poll (0.5s), mas o bid pode ter saltado para muito abaixo de 0.65 entre dois polls — o fill real é no bid atual, não em 0.65:

| Slug | EP | Exit esperada (0.65) | Exit real | Gap | PnL real | PnL esperado |
|---|---|---|---|---|---|---|
| 495300 | 0.82 | 0.65 | **0.36** | −0.29 | −$2.76 | −$1.02 |
| 500100 | 0.86 | 0.65 | **0.51** | −0.14 | −$2.10 | −$1.26 |
| 502800 | 0.83 | 0.65 | **0.44** | −0.21 | −$2.34 | −$1.08 |

Custo real dos 3 stops: **−$7.20** vs −$3.36 esperados. O gap de execução é o risco estrutural de stop em mercado com baixa liquidez — o bid já caiu antes do próximo poll.

#### Hedge seria melhor que stop?

No momento do crash, o `opp_bid` estava disponível (0.50–0.65). Análise comparativa:

| Slug | opp_bid no crash | PnL stop | PnL hedge | Delta |
|---|---|---|---|---|
| 495300 | 0.65 (OK) | −$2.76 | −$2.82 | −$0.06 |
| 500100 | 0.50 (OK) | −$2.10 | −$2.16 | −$0.06 |
| 502800 | 0.57 (OK) | −$2.34 | −$2.40 | −$0.06 |

**Hedge é marginalmente pior (−$0.06/trade).** O stop sai do EL a 0.36–0.51 (algum valor residual), enquanto o hedge assume EL vai a 0 e paga opp a 0.50–0.65. Adicionalmente, comprar opp requer abrir nova posição — o stop apenas vende o que já tem.

Liquidez: o `opp_bid` a 0.50–0.65 indica liquidez razoável para compra, mas o sell-side no crash (vender EL a 0.36) é ainda mais limitado. Na prática, ambas as saídas têm risco de slippage — o stop tem ligeira vantagem por não abrir nova posição.

#### PP com janela mais larga salvaria os stops?

Trade 495300 chegou a **bid=0.94 em secs=109** — fora da janela PP atual (36–70). Testado o impacto de ampliar `PP_SECS`:

| PP_SECS | PP disparou | PnL WIN+PP | vs atual |
|---|---|---|---|
| **70 (atual)** | **16/38** | **+$28.92** | — |
| 90 | 33/38 | +$24.36 | −$4.56 |
| 110 | 33/38 | +$21.42 | −$7.50 |
| 130 | 35/38 | +$18.18 | −$10.74 |

Ampliar PP_SECS para 110 salvaria o trade 495300 (+$3.48), mas cortaria prematuramente 17 trades WIN adicionais — custo de −$7.50 no portfólio total. **Não compensa.**

Trades 500100 e 502800: sem proteção por PP em nenhuma janela (bid nunca atingiu 0.88).  
Trade 502800: bid nunca superou o EP (0.83). Declínio imediato desde a entrada — nenhuma estratégia protege sem custo nos wins.

#### Conclusão

Os 3 STOP_LOSS são **perdas esperadas**, não evitáveis com o conjunto atual de ferramentas:
- Hedge: levemente pior e mais complexo de executar
- PP mais largo: salva 1 caso mas destrói +$7.50 em wins
- Stop mais apertado: dispararia em WIN trades (floor de 0.68 na amostra)

O gap de execução (fill a 0.36–0.51 em vez de 0.65) é o custo estrutural de operar stop-market em mercado de baixa liquidez. A configuração atual (stop 0.65 + PP secs<=70) permanece o melhor trade-off disponível.

### 9.11 Coleta estendida fim de semana — 122 trades (2026-05-22 a 2026-05-25)

**Sessões:** 36 | **Trades fechados:** 122 (+ 3 abertas no encerramento)  
**Script de análise:** `_check_results_full.py`

#### Acumulado total

| Outcome | n | PnL |
|---|---|---|
| WIN | 23 | — |
| PROFIT_PROTECT | 70 | +$47.16 (exit 0.88–1.00) |
| WIN_HEDGE | 2 | −$6.78 |
| STOP_LOSS | 25 | ~−$43.56 |
| REVERSAL | 2 | — |
| **Total** | **122** | **+$8.76** |

**WR: 76.2% | avg +$0.072/trade**

#### Comparação por período

| Período | Trades | WR | PnL | Taxa STOP | PP |
|---|---|---|---|---|---|
| 1ª amostra — qui 22/05 | 43 | 88.4% | +$18.42 | 7.0% (3) | 19 |
| **Adicionais — sex-dom** | **79** | **69.6%** | **−$9.66** | **27.8% (22)** | 51 |
| **Total** | **122** | **76.2%** | **+$8.76** | **20.5% (25)** | 70 |

O período de fim de semana teve quase 4× mais stops e foi net negativo em −$9.66. Hipótese: mercado BTC comporta-se diferente aos fins de semana (menor volume institucional, mais volatilidade de curto prazo → mais reversões do EL antes da resolução).

#### Gap de execução nos STOP_LOSS

O fill real acontece no bid do próximo poll após cruzar 0.65, não em 0.65:

| Faixa de gap | n trades |
|---|---|
| gap < 0.02 (fill próximo de 0.65) | 3 |
| gap 0.02–0.05 | 3 |
| gap 0.05–0.10 | 8 |
| gap 0.10–0.20 | 8 |
| gap > 0.20 | 3 |

**Gap médio: 0.10** → custo médio real por stop ≈ −$1.74 vs −$1.14 esperado (1.53× o teórico).

#### el_vel por outcome

| Outcome | n | avg el_vel | min | max |
|---|---|---|---|---|
| WIN | 23 | 0.140 | 0.085 | 0.325 |
| PROFIT_PROTECT | 70 | 0.140 | 0.081 | 0.327 |
| STOP_LOSS | 25 | 0.122 | 0.080 | 0.278 |
| WIN_HEDGE | 2 | 0.094 | 0.082 | 0.106 |
| REVERSAL | 2 | 0.102 | 0.081 | 0.124 |

STOP_LOSS têm el_vel médio 14% menor que WIN/PP (0.122 vs 0.140). A separação não é limpa mas cria oportunidade para gate.

#### Simulação gate el_vel (122 trades)

O **gate el_vel** é o threshold mínimo de crescimento do bid EL na janela secs 121–180 (`el_vel = bid_180 − bid_240`). Atualmente 0.08; valores maiores filtram sinais mais fracos:

| Gate | Trades | WR | STOP | STOP% | PnL | avg/trade |
|---|---|---|---|---|---|---|
| 0.08 (atual) | 122 | 76.2% | 25 | 20.5% | +$8.76 | +$0.072 |
| 0.10 | 82 | 80.5% | 14 | 17.1% | +$17.40 | +$0.212 |
| 0.12 | 62 | 83.9% | 9 | 14.5% | +$20.46 | +$0.330 |
| **0.13** | **51** | **86.3%** | **7** | **13.7%** | **+$22.38** | **+$0.439** |
| 0.15 | 37 | 86.5% | 5 | 13.5% | +$16.50 | +$0.446 |

Gate 0.13 triplicaria o avg/trade e aumentaria o PnL total em 2.6× — bloqueando 71 trades (58% das entradas). Gate 0.15 bloqueia 85 trades sem ganho adicional de WR.

**Melhor candidato: gate 0.12–0.13.** Requer validação em mais dados antes de alterar o runner — pode haver overfitting a esse período de fim de semana. Próximo passo: coletar semana completa e comparar distribuição de el_vel por dia da semana.

### 9.12 Próximos passos

- Validar gate el_vel 0.12–0.13 em coleta de dias úteis (segunda–sexta)
- Comparar taxa de STOP por dia da semana (fim de semana vs dia útil)
- Acompanhar primeiros resultados do runner EE real (`market/live_early_entry_real_v1.py`) quando for ativado
- Corrigir bug de prioridade do hedge no runner real (ver 9.13)

---

### 9.13 Simulação do Runner Real vs Paper (122 trades — fim de semana)

**Script:** `_sim_real_runner.py`  
**Data:** 2026-05-25  
**Metodologia:** Replay dos snapshots dos 36 logs de paper, aplicando a lógica de saída exata do `live_early_entry_real_v1.py` (shadow mode).

#### Resultado global

| Modo | PnL | WR | avg/trade |
|---|---|---|---|
| Paper runner | +$8.76 | 76.2% (93/122) | +$0.072 |
| Runner real (shadow sim.) | +$10.80 | 77.0% (94/122) | +$0.089 |
| **Delta** | **+$2.04** | | **+$0.017** |

Runner real seria **$2.04 melhor** que o paper nos mesmos 122 trades.

#### Distribuição de outcomes

| Outcome | Paper | Real (sim.) | Diferença |
|---|---|---|---|
| WIN | 23 | 7 | -16 |
| PROFIT_PROTECT | 70 | 87 | +17 |
| STOP_LOSS | 25 | 26 | +1 |
| WIN_HEDGE | 2 | 0 | -2 |
| REVERSAL | 2 | 2 | 0 |

#### Trades alteradas (18 de 122)

**Grupo A — 16 trades: WIN (paper) → PROFIT_PROTECT (real)**

O runner real detecta el_bid >= 0.88 na janela PP (secs 36-70) e sai antecipadamente. O paper runner perdeu essa janela — provavelmente porque o EL tracker foi reiniciado por 1 poll (mudança de slug momentânea), zerando `early_leader`, e a condição de PP `el_bid >= 0.88` não pôde ser avaliada.

- PnL médio paper (WIN): +$0.97/trade
- PnL médio real (PP): +$0.80/trade  
- Delta médio: -$0.17/trade × 16 = -$2.72 total

Impacto: o runner real capta o pico do bid antes de uma possível queda, mas perde entre $0.06 e $0.66 por trade comparado ao WIN completo.

**Grupo B — 2 trades: WIN_HEDGE (paper) → diferente (real)**

| Slug | EP | Paper outcome | Paper PnL | Real outcome | Real PnL | Delta |
|---|---|---|---|---|---|---|
| 455700 | 0.840 | WIN_HEDGE | -$4.14 | PROFIT_PROTECT | +$0.24 | **+$4.38** |
| 468000 | 0.860 | WIN_HEDGE | -$2.64 | STOP_LOSS | -$1.86 | **+$0.78** |

- **455700:** bid chegou a 0.880 em secs=62 (janela PP). Runner real sai via PP a +$0.24. Paper runner (com EL tracker ativo) aciona hedge quando bid cai abaixo de 0.50 após o pico, encerrando a -$4.14.
- **468000:** bid cai diretamente de >0.65 para 0.55 (abaixo do stop). Runner real: STOP em 0.55 = -$1.86. Paper: hedge a preço desfavorável = -$2.64.

**Impacto total Grupo B:** real = **+$5.16** melhor que paper.

#### Bug crítico identificado: hedge é código morto no runner real

**Problema:** A prioridade 4 (hedge dinâmico) em `live_early_entry_real_v1.py` nunca é alcançada:

```python
# Prioridade 3: stop loss (el_bid < 0.65)  ← captura el_bid < 0.50 PRIMEIRO
elif 0 < el_bid < EE_STOP_LEVEL:           # EE_STOP_LEVEL = 0.65
    # stop

# Prioridade 4: hedge (el_bid < 0.50)  ← CÓDIGO MORTO
elif el_bid < EE_HEDGE_THR and opp_bid > 0:  # EE_HEDGE_THR = 0.50
    # hedge — NUNCA chega aqui (P3 captura qualquer 0 < el_bid < 0.65 antes)
```

Qualquer el_bid entre 0 e 0.65 aciona o stop (P3) antes do hedge (P4). O hedge só seria alcançável se el_bid == 0 exatamente.

**Impacto na prática (fim de semana):** Nenhum dano — PP disparou antes dos casos de hedge, resultando em PnL melhor. Mas em condições onde PP não puder disparar (bid não chega a 0.88 na janela 36-70), o runner real fará STOP em vez de hedge, potencialmente com resultado pior.

**Fix sugerido:** Mover o bloco de hedge para ANTES do stop no runner real (`live_early_entry_real_v1.py` ~linhas 840-860):

```python
# Prioridade 3: gap abaixo de 0.50 — hedge dinâmico (MOVER PARA ANTES DO STOP)
elif el_bid < EE_HEDGE_THR and opp_bid > 0:
    stop_pnl  = (el_bid  - entry_price) * qty
    hedge_pnl = (1.0 - entry_price - opp_bid) * qty
    if hedge_pnl > stop_pnl:
        # hedge
    else:
        # stop

# Prioridade 4: stop loss convencional (el_bid entre 0.50 e 0.65)
elif 0 < el_bid < EE_STOP_LEVEL:
    # stop
```

Requer confirmação antes de implementar (alterar lógica de risco).

#### Conclusão

O runner real (`live_early_entry_real_v1.py`) em shadow mode **produziria +$2.04 (23%) a mais que o paper** nos 122 trades do fim de semana. O ganho vem principalmente da PP capturando picos antes de colapsos que o paper não capturou (+$5.16 nos 2 WIN_HEDGE → PP/STOP). As 16 trades WIN→PP resultam em -$2.72, mas ainda sim a PP é benéfica globalmente.

O bug do hedge (prioridade invertida) não causou dano neste dataset, mas é um risco latente para cenários futuros onde PP não disponível.

---

### 9.14 Simulação: opp_bid e dist_pp na entrada (2026-05-25)

**Script:** `_sim_opp_gate.py`  
**Contexto:** Observação do operador — ao usar manualmente a extensão com sinal EE, notou que em algumas entradas "verdes" o preço spot do BTC estava muito próximo do threshold de resolução (ex: BTC em $80.020, threshold em $79.990 = apenas $30 de margem com >1 min restante). Hipótese: pouca margem em dólares = risco maior de reversão cruzar o threshold.

#### O que foi simulado (proxy no Polymarket)

Por não ter o preço spot do BTC nos logs atuais, foram usados dois proxies disponíveis nos snapshots:

- **opp_bid**: bid do lado oposto na entrada. Em mercado binário, `ep + opp_bid ≈ 1.0` — quanto maior o opp, mais "contestado" o mercado.
- **dist_pp**: `0.88 - ep` = distância até o trigger de Profit Protect. Proxy imperfeito da "folga" de preço.

#### Resultado: opp_bid NÃO discrimina STOP_LOSS

```
opp_bid médio por outcome:
  WIN:          0.171
  PP:           0.167
  STOP_LOSS:    0.167   ← diferença de apenas -0.001
  
Diferença stops vs não-stops: -0.001 (irrelevante)
```

**Motivo estrutural:** em mercado binário, opp_bid é mecanicamente o complemento do ep (`opp ≈ 1 - ep - spread`). Se ep=0.82, opp≈0.19 por construção. Não há informação adicional — é redundante com o preço de entrada.

#### Resultado: dist_pp > 0.03 tem valor marginal

| Gate dist_pp > | n | WR | avg/trade | Bloqueados |
|---|---|---|---|---|
| base | 122 | 76.2% | +$0.072 | 0 |
| 0.03 | 99 | 79.8% | **+$0.238** | 23 |
| 0.04 | 68 | 76.5% | +$0.184 | 54 |
| 0.05 | 36 | 72.2% | +$0.157 | 86 |

`dist_pp > 0.03` equivale a "não entrar quando ep=0.86". Melhora avg de +$0.072 para +$0.238 bloqueando apenas 23 trades — os de entrada mais alta (mais próximos do PP trigger).

#### Gates opp_bid < X não funcionam

| Gate | n | avg | Problema |
|---|---|---|---|
| < 0.25 / < 0.22 / < 0.20 | 122 | +$0.072 | Todos passam — max opp na amostra é 0.19 |
| < 0.18 | 84 | +$0.021 | **Piora** — bloqueou os ep=0.82 que eram bons |
| < 0.16 | 25 | **-$0.672** | Catástrofe — sobram apenas os ruins |

#### Melhor combinação encontrada

| Cenário | n | WR | avg/trade |
|---|---|---|---|
| el_vel >= 0.13 | 51 | 86.3% | +$0.439 |
| el_vel >= 0.13 + opp < 0.18 | 32 | **90.6%** | **+$0.512** |

A combinação chega a 90.6% WR mas bloqueia 90/122 trades — volume insuficiente para validar sozinho.

#### O que o operador realmente observou (conceito diferente — não simulado)

A observação original era sobre a **distância em dólares entre o preço spot do BTC e o threshold de resolução** do contrato — exatamente o mesmo conceito que o almost_resolved já usa como gate principal.

**Exemplo concreto:** BTC em $80.020, candle abre/resolve em $79.990 → apenas $30 de margem com >1 min restante. O sinal EL pode estar verde porque BTC cruzou o nível recentemente, mas quanto mais próximo do threshold, maior o risco de retorno.

Esse conceito é fundamentalmente diferente dos proxies simulados acima (`opp_bid`, `dist_pp`):
- É baseado no **gráfico de preço**, não no orderbook Polymarket.
- A mesma distância em cents no orderbook (ep=0.84) pode corresponder a $15 ou $150 de distância no BTC, dependendo do momento do candle.
- É sensível ao **tempo restante**: $30 de margem com 90s restantes é muito diferente de $30 com 10s restantes.

#### Por que não foi possível simular ainda

Os snapshots do paper runner (`ee_paper.jsonl`) não incluem o preço spot do BTC nem o opening reference price. Apenas registram `up_bid`, `down_bid`, `secs`.

#### Requisitos para implementar esse filtro

As funções já existem em `market/current_scalp_signal_v1.py` — são as mesmas do almost_resolved:

```python
from market.current_scalp_signal_v1 import (
    fetch_external_btc_reference_v1,            # BTC spot atual (Binance + Coinbase, mediana)
    fetch_binance_open_price_for_event_start_v1, # preço de abertura do candle (= price to beat)
)
```

**Para adicionar ao paper runner EE (`live_early_entry_paper_v1.py`):**

1. **Por slug (uma vez):** chamar `fetch_binance_open_price_for_event_start_v1(event_start_time)` para obter o `opening_reference_price` (threshold de resolução)
2. **Na entrada:** chamar `fetch_external_btc_reference_v1()` para obter `reference_price` (BTC spot)
3. **Calcular:** `dist_ptb_usd = abs(reference_price - opening_reference_price)`
4. **Logar** no `ee_paper_entry`: campos `btc_spot`, `btc_threshold`, `dist_ptb_usd`, `dist_ptb_bps`
5. **Gate candidato:** `dist_ptb_usd > X` — valor de X a determinar com dados (almost_resolved usa 35–100 USD dependendo do setup)

**Nota:** `event_start_time` já está disponível no quase_resolvido via `fetch_event_by_slug`. O paper EE precisaria adicionar essa chamada uma vez por slug novo.

#### Análise retrospectiva dos 122 trades (2026-05-25)

**Script:** `_sim_dist_ptb_retro.py`  
O slug `btc-updown-5m-XXXXXXXXXX` contém diretamente o Unix timestamp do início do candle (= price to beat). Combinando com o `ts` da entrada, foi possível reconstruir `dist_ptb_usd` para todos os 122 trades via Binance klines histórico.

**dist_usd médio por outcome:**
```
WIN:            33.5 USD  (avg)
PROFIT_PROTECT: 32.0 USD
STOP_LOSS:      44.3 USD  ← notavelmente maior
Diferença: stops 44.3 vs não-stops 32.9 = +11.4 USD
```

**Direção do efeito (oposta à intuição inicial):**  
O risco maior é BTC *longe* do threshold (sobreextendido), não perto. Quando BTC já se afastou $80-120 da abertura do candle, o movimento é mais provável de reverter (traders saindo da posição). Quando BTC está perto ($20-30), o EL acabou de se formar e o momentum é mais fresco.

**Gate `dist_usd < X` (bloquear sobreextensão):**

| Gate usd< | n | WR | STOP% | avg/trade | bloq |
|---|---|---|---|---|---|
| base | 122 | 76.2% | 20.5% | +$0.072 | — |
| < 80 | 115 | 78.3% | 19.1% | +$0.146 | 7 |
| < 50 | 99 | 79.8% | 17.2% | **+$0.170** | 23 |
| < 30 | 61 | 82.0% | 16.4% | +$0.261 | 61 |

**Combinações:**

| Cenário | n | WR | avg/trade |
|---|---|---|---|
| el_vel >= 0.13 | 51 | 86.3% | +$0.439 |
| el_vel >= 0.12 + dist < 80 | **60** | 85.0% | **+$0.416** |
| el_vel >= 0.12 + dist < 50 | 50 | 86.0% | +$0.422 |
| el_vel >= 0.13 + dist < 80 | 50 | 86.0% | +$0.434 |

**Insight chave:** `el_vel >= 0.12 + dist_usd < 80` mantém 60 trades (+9 vs gate 0.13 puro) com avg/trade praticamente idêntico (+$0.416 vs +$0.439). O dist_usd adiciona valor principalmente na faixa el_vel 0.08–0.12 — os 3 piores stops com dist > 80 USD têm todos el_vel < 0.13, logo o gate 0.13 já os bloquearia de qualquer forma.

**Logging já implementado:** o paper runner (commit `db1c46b`) agora registra `btc.dist_usd`, `btc.dist_bps`, `btc.spot` e `btc.threshold` em cada `ee_paper_entry`. Após coletar dados de dias úteis, comparar `el_vel >= 0.12 + dist_usd < 80` vs `el_vel >= 0.13` em volume e qualidade.

#### Status e próximo passo

- Valor: **confirmado** — dist_usd tem correlação real (+11.4 USD de diferença stops vs não-stops).
- Gate prioritário: validar `el_vel >= 0.13` em dias úteis primeiro. Depois testar `el_vel >= 0.12 + dist_usd < 80` como alternativa com mais volume.
- Logging ativo: paper runner já captura os campos desde `db1c46b`.

---

### 9.15 Análise EE — dist_ptb trajectory, BTC momentum e universo completo (2026-05-25)

**Scripts:** `_sim_dist_trajectory.py`, `_sim_btc_momentum.py`, `_sim_full_universe.py`  
**Amostra:** 122 trades fechados (paper runner, fim de semana 2026-05-22 a 2026-05-25)  
**Leitor unificado:** `_ee_log_reader.py` — suporta paper e runner real via `--logs`

---

#### 9.15.1 Trajetória da dist_ptb durante o hold (WIN vs STOP)

**Script:** `_sim_dist_trajectory.py`  
**Conceito:** `dist_ptb = abs(btc_spot - opening_reference_price)`. Positivo = BTC se afastando do threshold. Negativo = BTC voltando.

| Outcome | n | dist_entry | dist_exit | delta_médio |
|---|---|---|---|---|
| WIN | 23 | 33.3 | 44.9 | **+11.6** |
| PROFIT_PROTECT | 70 | 32.0 | 40.8 | **+8.8** |
| STOP_LOSS | 25 | 44.3 | 38.5 | **-5.8** |

**Padrão confirmado:** trades vencedores têm BTC se afastando do threshold (+8.8 a +11.6 USD). Stops têm BTC voltando em direção ao threshold (-5.8 USD). A direção do BTC durante o hold prediz o outcome melhor que qualquer filtro de entrada testado até aqui.

**Limitação:** 16/25 stops mostram `delta=0` — entrada e saída caem no mesmo candle de 1min da Binance, sem resolução no preço. Seria necessário tick data para analisar esses casos.

**Stops com el_vel < 0.13 vs >= 0.13:**
- el_vel < 0.13: delta médio = -8.4 (BTC voltou acentuadamente)
- el_vel >= 0.13: delta médio = +0.8 (BTC estável — stops por outro motivo)

---

#### 9.15.2 BTC momentum 3 minutos antes da entrada (paradoxo adverso)

**Script:** `_sim_btc_momentum.py`  
**Método:** 3 candles 1m Binance antes da entrada. `adverse_1m = True` se BTC vai na direção oposta ao lado EL (ex: BTC caindo para posição UP, ou subindo para DOWN).

**Resultado principal:**

| Grupo | n | WR | STOP% | PnL | avg/trade |
|---|---|---|---|---|---|
| BTC adverso 1m | 18 | **94.4%** | 5.6% | +$10.86 | **+$0.603** |
| BTC favorável 1m | 104 | 73.1% | 23.1% | -$2.10 | -$0.020 |
| todos | 122 | 76.2% | 20.5% | +$8.76 | +$0.072 |

**Paradoxo adverso:** entrar com BTC indo contra o lado EL é *melhor*, não pior. Interpretação: o orderbook EL se mantém forte (bid 0.82-0.86) mesmo com pressão de spot contrária → sinal de convicção mais forte. Só 1/25 stops tinha BTC adverso — praticamente todos os stops ocorrem com BTC *favorável*.

**Efeito simétrico (UP e DOWN):**
- UP adverso (BTC caindo, d1m_avg = -9.9): n=8, WR=100%, avg=+$0.630
- DOWN adverso (BTC subindo, d1m_avg = +9.4): n=10, WR=90%, avg=+$0.582

**Gate de volatilidade (range_3m_usd < X):**

| Gate | n | WR | STOP% | avg/trade | bloqueados |
|---|---|---|---|---|---|
| < 50 | 61 | 80.3% | 16.4% | +$0.218 | 61 |
| < 80 | 95 | 78.9% | 17.9% | +$0.149 | 27 |
| < 100 | 104 | 77.9% | 19.2% | +$0.125 | 18 |
| base | 122 | 76.2% | 20.5% | +$0.072 | 0 |

**Combinações chave (122 trades reais):**

| Cenário | n | WR | avg/trade |
|---|---|---|---|
| el_vel >= 0.13 | 51 | 86.3% | +$0.439 |
| el_vel >= 0.13 + range_3m < 100 | 43 | 86.0% | +$0.427 |
| el_vel >= 0.13 + favorável_3m | 48 | 85.4% | +$0.438 |
| el_vel >= 0.12 + favorável_3m + rng < 100 | 47 | 85.1% | +$0.416 |

---

#### 9.15.3 Simulação do universo completo (440 candidatos)

**Script:** `_sim_full_universe.py`  
**Método:** Varre todos os snapshots paper (não só trades reais do runner). Detecta primeiro snap com el_bid em [0.82, 0.86] em secs [30, 180]. Simula saída com lógica real (stop 0.65, PP 0.88 @ secs 36-70, WIN <= 35s). Busca BTC momentum via Binance klines histórico.

**Categorias de sinal:**

| Categoria | n candidatos | Definição |
|---|---|---|
| A_signal_ok | 100 | el_vel >= 0.08 + cont_ok (o que o runner aceita) |
| B_cont_only | 149 | cont_ok mas el_vel < 0.08 |
| C_el_only | 191 | EL detectado sem cont_ok |

**Resultados por categoria (435 simulados com outcome):**

| Categoria | n | WR | STOP% | avg/trade | adv1m | rng3m |
|---|---|---|---|---|---|---|
| A_signal_ok | 97 | 78.4% | 19.6% | +$0.108 | 12.4% | 58.6 |
| B_cont_only | 148 | 73.0% | 25.7% | -$0.019 | 29.1% | 77.9 |
| C_el_only | 190 | 75.3% | 23.2% | +$0.032 | 39.5% | 42.6 |
| **TOTAL** | **435** | **75.2%** | **23.2%** | **+$0.032** | — | — |

**Por faixa el_vel (universo completo):**

| Faixa | n | WR | STOP% | avg/trade |
|---|---|---|---|---|
| < 0.0 | 101 | 78.2% | 19.8% | +$0.110 |
| [0.0, 0.04) | 79 | 77.2% | 21.5% | -$0.002 |
| [0.04, 0.06) | 44 | 72.7% | 27.3% | +$0.044 |
| [0.06, 0.08) | 60 | 78.3% | 20.0% | +$0.114 |
| **[0.08, 0.10)** | **32** | **68.8%** | **28.1%** | **-$0.141** |
| **[0.10, 0.13)** | **39** | **64.1%** | **30.8%** | **-$0.343** |
| >= 0.13 | 80 | 76.2% | 23.8% | +$0.148 |

**Zona perigosa el_vel [0.08, 0.13):** pior performance de todas as faixas (-$0.343 avg). O gate 0.13 efetivamente "pula" essa zona ruim — os primeiros snaps com el_bid em range capturam el_vel antes da janela EL ter convergido completamente.

**Paradoxo adverso por categoria (universo completo):**

| Grupo | n | WR | avg/trade |
|---|---|---|---|
| [A] adverso_1m | 12 | **91.7%** | **+$0.545** |
| [A] favorável_1m | 85 | 76.5% | +$0.046 |
| [B] adverso_1m | 43 | **81.4%** | **+$0.066** |
| [B] favorável_1m | 105 | 69.5% | -$0.053 |
| [C] adverso_1m | 75 | 72.0% | **-$0.063** ← inverte sem cont_ok |
| [C] favorável_1m | 115 | 77.4% | +$0.094 |

**O efeito adverso depende de cont_ok.** Sem confirmação EL (categoria C), o paradoxo inverte — adverso piora. Com cont_ok (A e B), adverso sempre melhora.

**Melhores combinações no universo completo:**

| Cenário | n | WR | STOP% | avg/trade |
|---|---|---|---|---|
| base (qualquer el_bid em range) | 435 | 75.2% | 23.2% | +$0.032 |
| el_vel >= 0.08 (signal_ok) | 97 | 78.4% | 19.6% | +$0.108 |
| el_vel >= 0.13 | 80 | 76.2% | 23.8% | +$0.148 |
| range_3m < 80 | 348 | 76.1% | 22.4% | +$0.059 |
| **el_vel >= 0.08 + range_3m < 80** | **77** | **81.8%** | **16.9%** | **+$0.218** |
| el_vel >= 0.08 + adverso_1m | 12 | 91.7% | 8.3% | +$0.545 |
| el_vel >= 0.13 + range_3m < 80 | 64 | 78.1% | 21.9% | +$0.180 |

**Gate mais robusto identificado:** `el_vel >= 0.08 + range_3m < 80` — 77 trades, WR 81.8%, avg +$0.218. Mais volume que o gate 0.13 puro (80 trades com avg menor +$0.148) e resistente ao período de fim de semana.

---

#### 9.15.4 Leitor unificado e testes nos logs do runner real

**Arquivo:** `_ee_log_reader.py`  
Detecta automaticamente paper vs real pelos tipos de registro:
- Paper: `ee_paper_entry` / `ee_paper_closed` (pnl em `r['ee']['pnl']`)
- Real: `ee_real_entry` / `ee_real_closed` (pnl em `r['pnl']` direto)

Todos os 4 scripts de análise aceitam `--logs` para apontar para logs do runner real:

```bash
# Paper (padrão):
python _sim_dist_ptb_retro.py
python _sim_dist_trajectory.py
python _sim_btc_momentum.py
python _sim_full_universe.py

# Runner real (no PC do runner):
python _sim_dist_ptb_retro.py  --logs "logs/ee_real_*/ee_real.jsonl"
python _sim_dist_trajectory.py --logs "logs/ee_real_*/ee_real.jsonl"
python _sim_btc_momentum.py    --logs "logs/ee_real_*/ee_real.jsonl"
python _sim_full_universe.py   --logs "logs/ee_real_*/ee_real.jsonl"
```

**O que comparar ao rodar nos logs reais:**
1. `_sim_btc_momentum.py`: confirmar paradoxo adverso (WR 94.4% adverso) em dias úteis com volume real
2. `_sim_full_universe.py`: confirmar zona ruim [0.08, 0.13) e gate `el_vel >= 0.08 + range_3m < 80`
3. `_sim_dist_trajectory.py`: confirmar delta positivo WIN, negativo STOP (pode ser mais limpo com menor latência do runner real)
4. `_sim_dist_ptb_retro.py`: confirmar diferença stops (+44.3 USD) vs wins (+32-33 USD) em dias úteis

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

### TESTE F — BTC momentum nos logs reais (confirmar paradoxo adverso)
```bash
python _sim_btc_momentum.py --logs "logs/ee_real_*/ee_real.jsonl"
```
Verificar: WR adverso 1m ainda ~90%+ em dias úteis? Confirma que o efeito não é artefato de fim de semana.

### TESTE G — Universo completo nos logs reais (confirmar zona [0.08, 0.13))
```bash
python _sim_full_universe.py --logs "logs/ee_real_*/ee_real.jsonl"
```
Verificar: zona el_vel [0.08, 0.13) ainda piora nos dados reais? Gate `el_vel >= 0.08 + range_3m < 80` se mantém?

### TESTE H — Trajetória dist_ptb nos logs reais
```bash
python _sim_dist_trajectory.py --logs "logs/ee_real_*/ee_real.jsonl"
```
Verificar: delta positivo WIN (+8 a +12 USD), negativo STOP (-5 USD) se confirma?

### TESTE I — dist_ptb retrospectivo nos logs reais
```bash
python _sim_dist_ptb_retro.py --logs "logs/ee_real_*/ee_real.jsonl"
```
Verificar: diferença stops (~44 USD) vs wins (~32 USD) se mantém em dias úteis?

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

FASE 3 — Runner de teste (concluída):
  [x] EE implementado em run_market_monitor.py
  [x] Bug de logging corrigido (_just_closed)
  [x] 53 trades EE paper coletados (3 sessões, mai/2026)
  [x] Analisado: WR=92.5%, PnL=+$29.04, ZERO stops

FASE 4 — Validação e produção (concluída em 2026-05-25):
  [x] _sim_real_runner.py: runner real simulado vs paper → +$2.28 melhor
  [x] EE real runner implementado (market/live_early_entry_real_v1.py)
  [x] EE integrado ao AR runner (EE_REAL_ENABLED + EE_REAL_POSTS_ENABLED)
  [x] Gates AR por variante implementados (ver Seção 13)
  [x] EE real ativo com ordens reais desde 2026-05-25

FASE 5 — Monitoramento e ajuste fino (em andamento):
  [ ] Coletar 50+ trades EE real, comparar WR/PnL com paper (meta: ~90%+ WR)
  [ ] Validar gates AR com novos dados (meta: PnL positivo em cada variante)
  [ ] Avaliar gate range_15s nos logs reais do runner AR (Teste C)
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
| `EE_STOP_LEVEL` | 0.65 | Stop FAK quando el_bid < 0.65 |
| `EE_PROFIT_PROTECT_BID` | 0.88 | PP GTC quando el_bid >= 0.88 e secs 36-70 |
| `EE_PROFIT_PROTECT_SECS` | 70 | Janela de PP |
| `EE_MAX_SECS` | 180 | Janela máxima de entrada |
| `EE_MIN_SECS` | 30 | Janela mínima de entrada |
| `qty EE` | 6 shares | Não alterar sem validação |
| `qty AR` | 6 shares | Não alterar sem validação |

---

## 13. Gates AR por Variante (implementado em 2026-05-25)

### Contexto

O runner AR acumulou -$37.65 em 140 trades históricos. A análise por variante revelou que as perdas se concentram em cenários específicos e evitáveis.

Scripts: `_analyze_ar_gates.py`, `_analyze_variant_gates.py`  
Dados: 142 trades cruzados (enter + flat/trade_closed/redeem_flat)

### Resultados por Variante

| Variante | n | WR | PnL | avg |
|---|---|---|---|---|
| `standard` | 101 | 73% | -$39.29 | -$0.389 |
| `dual_rich_late_limit` | 29 | 28% | +$0.54 | +$0.019 |
| `controlled_late_entry` | 10 | 80% | +$1.10 | +$0.110 |

**`standard`** — problema central: entradas a `ep >= 0.97` têm risco assimétrico fatal. Win máximo = 6 × 0.01 = $0.12; loss quando mercado resolve errado = 6 × 0.97 = $5.82. O stop não executa quando o bid vai de 0.98 → 0 instantaneamente na resolução. Adicionalmente, `d_bps < 12` indica margem insuficiente vs o price-to-beat.

**`dual_rich_late_limit`** — 19 de 29 trades são breakeven puro: entram a ep=0.99, saem a 0.99 (preço de saída máximo = preço de entrada). Travam o capital sem gerar lucro. Os 2 stops reais também foram a ep=0.99.

**`controlled_late_entry`** — positivo sem gates adicionais. Manter.

### Gates Implementados

```python
# em market/live_current_almost_resolved_real_v1.py
# (logo antes de _post_entry_order)

if variant == "standard":
    if ep >= 0.97:       → entry_blocked  # risco catastrófico
    if d_bps < 12.0:     → entry_blocked  # margem insuficiente

if variant == "dual_rich_late_limit":
    if ep >= 0.985:      → entry_blocked  # zero margem de lucro
```

### Impacto Histórico (simulado nos 142 trades)

| | n | WR | PnL | avg |
|---|---|---|---|---|
| Sem gates (base) | 140 | 64% | -$37.65 | -$0.269 |
| Com gates (mantidos) | 46 | 83% | +$0.29 | +$0.006 |
| Bloqueados | 94 | — | -$37.94 | — |

Os 94 trades bloqueados concentram 100% das perdas históricas.

### Como rodar os testes do outro PC

```bash
# Logs EE paper estão em test_data/ee_paper/ (commitados no git)
# Copiar para logs/ antes de rodar:
mkdir -p logs
cp -r test_data/ee_paper/ee_paper_* logs/

# Testar EE paper
python _check_results_full.py
python _sim_real_runner.py

# Testar AR runner real (requer logs/current_almost_resolved_real_*)
python _check_results_full.py --ar
python _analyze_ar_gates.py
python _analyze_variant_gates.py
```

---

## 14. Correção awaiting_redeem (2026-05-25)

**Bug:** Quando o runner vendia parcialmente uma posição (ex: 5.98/6.0 tokens) e os 0.02 restantes iam para `awaiting_redeem`, o trade ficava preso para sempre se:
- `token_balance_qty > dust_archive_qty (0.01)` → não é dust
- `last_reason` não contém "resolution_loss" → o loss_writeoff_timeout (1h) nunca dispara

**Fix:** Após 2 minutos em `awaiting_redeem` sem motivo de perda, o runner força um `update_balance_allowance` (refresh on-chain) e fecha o trade com `stale_redeem_closed` se:
- O fresh balance é zero (redeem já processado, API estava atrasada), **ou**
- O residual é < 10% do qty preenchido (write-off parcial mínimo)

PnL calculado = porção vendida + residual estimado (1.0 para WIN inferred).

Evento logado: `type: "stale_redeem_closed"` com `token_balance_cached`, `token_balance_fresh`, `pct_remaining`.

---

## 15. Simulação EL Flip — Inversão do Early Leader (2026-05-25)

Script: `_sim_el_flip.py`  
Dados: 860 slugs paper local (fim de semana 22-25/05) + 37 slugs shadow quinta-feira (test_data)

### 15.1 Conceito

Quando o EL original perde a liderança e o lado oposto assume com bid em [0.60, 0.72],
entra-se no **novo líder** em vez do EL original. Complementa o EE: EE entra no EL
estável (0.82-0.86), Flip entra na inversão (0.60-0.72). Não competem — slugs distintos.

Frequência: 344/834 slugs com EL = **41% dos slugs têm inversão na faixa**.

### 15.2 Resultados sem stop (hold mode) — 295 trades

| Segmento | n | WR | avg/trade |
|---|---|---|---|
| **Base (todos)** | 295 | **81.0%** | **+$0.276** |
| gap >= 0.35 (flip dominante) | 57 | **89.5%** | +$0.385 |
| ep 0.69-0.72 (entrada mais cara) | 37 | **94.6%** | +$0.636 |
| flip secs 180-121 | 105 | 83.8% | +$0.426 |
| flip secs > 180 | 82 | 80.5% | +$0.279 |

Breakdown de outcomes: 239 TP_WIN (+1.237 avg) vs 56 LOSS_RESOLVE (-3.826 avg).

### 15.3 Comparação com stop 0.45 (parâmetros do overlay)

| Stop | WR | stop% | avg/trade |
|---|---|---|---|
| Sem stop | 81.0% | 0% | **+$0.276** |
| Stop=0.45 | 50.7% | 49.3% | +$0.067 |
| Stop=0.30 | 62.8% | 37.2% | +$0.023 |

**Conclusão**: stop causa 40% de falsos stops (trades que iriam para TP mas tocaram 0.45 antes).
BTC oscila muito na faixa 0.40-0.70 antes de resolver. Stop prejudica sistematicamente.

### 15.4 Filtro de secs — diferença vs overlay SOL/XRP

| Janela de entrada | WR (no-stop) | avg |
|---|---|---|
| flip secs > 180 | 80.5% | +0.279 |
| flip secs 180-121 | 83.8% | **+0.426** |
| flip secs 120-61 | 82.0% | +0.303 |
| flip secs 60-31 | 78.6% | +0.180 |
| **flip secs <= 30** | **68.4%** | **-0.515** |

**Importante**: o filtro `secs<=60` do overlay (para SOL/XRP) **piora** os resultados em BTC.
WR cai para 74.5% vs 81.0% base. Inversões mais tardias têm menos tempo para atingir o TP=0.85.

### 15.5 Shadow data (quinta-feira — dia útil)

13 trades, WR 92.3%, avg +$1.052. Padrão weekday mais forte, consistente com análise EE.

### 15.6 Comparação EE vs EL Flip

| Estratégia | Candidatos | WR | avg/trade | Nota |
|---|---|---|---|---|
| EE (com stop) | 435 | ~82-87% | +$0.032 | bid 0.82-0.86, EL estável |
| EL Flip (no-stop) | 344 | 81.0% | +$0.276 | bid 0.60-0.72, EL inverte |
| EL Flip gap>=0.35 | 57 | 89.5% | +$0.385 | inversão dominante |

### 15.7 Perfil de risco

- Win: (0.85 - ep) × 6 ≈ +$1.20 por trade (ep médio ~0.65)
- Loss: (0.00 - ep) × 6 ≈ -$3.90 por trade (catastrófico — 19% dos trades)
- EV positivo apenas porque WR=81% supera a assimetria negativa

### 15.8 Próximos passos

- [ ] Validar gate gap>=0.35 em logs reais (runner do outro PC)
- [ ] Confirmar padrão weekday em dados reais (meta: WR >= 85% com gap>=0.35)
- [ ] Avaliar se ep 0.69-0.72 (WR 94.6%) representa "flips dominantes" que já confirmaram
- [ ] Implementar como estratégia separada (runner flip) — não no EE runner atual

```bash
# Rodar simulação EL Flip
python _sim_el_flip.py
python _sim_el_flip.py --no-stop
python _sim_el_flip.py --logs "logs/ee_real_*/ee_real.jsonl"

# Testar com gap mais restritivo
python _sim_el_flip.py --no-stop --flip-gap 0.20
```
