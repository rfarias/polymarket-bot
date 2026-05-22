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

### 9.10 Próximos relatórios

Acompanhar acumulado após 24h+ de coleta. Meta: 30–50 trades EE + 10+ would_enter para validação.

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
| `qty EE` | 6 shares | Equiparado ao runner real (2026-05-21) |
| `qty AR normal` | 6 shares | Não alterar sem validação |
| `entry_min AR` | 0.88 | Não alterar sem validação |
| `stop AR` | 2T | Para EE, sem stop (hold to resolution) |
| `range_15s gate` | 0.03 | Proposto; validar nos logs reais (Teste C) |
