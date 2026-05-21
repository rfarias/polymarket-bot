# Testes e Análises — Early Leader + AR Filtros

Data: 2026-05-21  
Logs analisados: 661 slugs resolvidos (monitor + paper + runner logs locais)  
Scripts: `analyze_early_leader_all_logs.py`, `analyze_entry_threshold_and_gaps.py`, `analyze_monitor_pnl.py`

---

## 1. Early Leader — Achados Consolidados

### 1.1 Poder preditivo geral

| Condição | n | Acerto direcional |
|---|---|---|
| EL >= 0.55 (qualquer) | 545/661 | 79,1% |
| EL >= 0.65 | 376 | 83,8% |
| EL >= 0.70 | 296 | 87,8% |
| EL + F3 continuidade (>= 0.70 em secs 181-120) | 299 | **94,3%** |

**F3 = maior filtro de qualidade disponível nos dados atuais.**

### 1.2 Inversão do Early Leader

Quando o líder identificado em secs 240-181 troca de lado numa janela posterior:

| Situação | n | EL original acerta | Novo líder acerta |
|---|---|---|---|
| EL estável até secs 60 | 445 | **92,6%** | 7,4% |
| EL inverte em 180-121s | 38 | 18,4% | **81,6%** |
| EL inverte em 120-61s | 62 | 19,4% | **80,6%** |

**Conclusão:** a inversão do EL é sinal de reversão tão forte quanto o EL original é sinal de continuidade.

Por intensidade do bid no momento da inversão:

| Bid no momento da inversão | n | Novo líder vence |
|---|---|---|
| < 0.60 (EL fraco inverteu) | 34 | 68% |
| 0.60–0.72 (EL médio inverteu) | 41 | **90%** |
| >= 0.72 (EL forte inverteu) | 25 | **84%** |

---

## 2. Entry Threshold vs EL Estável

### 2.1 Hipótese testada
Com EL estável garantindo direção em 94%, poderíamos entrar mais barato (ep 0.82-0.86) para ganhar mais por win?

### 2.2 Resultado (stop=2T, qty=6)

| Filtro | ep>= | n | WR% | PnL total | avg/trade |
|---|---|---|---|---|---|
| Baseline | 0.82 | 659 | 61,3% | +$52,87 | +$0,080 |
| Baseline | 0.88 | 641 | 67,9% | **+$65,47** | +$0,102 |
| EL estável | 0.82 | 301 | 67,8% | +$22,45 | +$0,075 |
| EL estável | 0.86 | 300 | 69,7% | +$23,71 | +$0,079 |
| EL estável | 0.88 | 298 | 71,5% | **+$26,59** | +$0,089 |

**Conclusão:** baixar entry_min com EL estável não melhora PnL. O stop de 2T mata as entradas precoces por volatilidade antes da resolução. A faixa 0.82-0.86 tem 89% de acerto direcional *sem stop*, mas o stop de curto prazo come o ganho.

### 2.3 Achado colateral importante
Sem stop (hold to resolution), a faixa ep 0.82-0.86 rende $+90,90 em 262 trades (89% acerto). Isso abre a hipótese:

> **Usar EL estável como gate para entrar em ep 0.82-0.86 SEM STOP (hold to resolution), aproveitando que a direção está quase garantida.**

Isso é uma variante nova, ainda não testada com dados reais. Requer validação nos logs do runner real.

---

## 3. Book Gaps e Quedas Bruscas

### 3.1 O que são na prática
Não é falta de liquidez: depth médio antes da queda é 37.000 (alto). São reversões bruscas de preço em 1 snap (poll).

### 3.2 Características das quedas do winner bid (n=495)

| Campo | Valor |
|---|---|
| Drop médio | 0,176 (17,6 cents) |
| Drop máximo | 0,760 |
| Secs médio | 165s (acontecem em qualquer janela) |
| Depth médio antes | 37.092 (não é liquidez) |
| range_15s médio antes | **0,0699** (mercado oscilando) |
| Velocidade 30s antes | **positiva** (+0,0033) — bid subia antes de cair |

### 3.3 Gate potencial: range_15s

`range_15s >= 0.03` captura **67% das quedas do winner** (quando o campo está presente nos logs).  
Falsos bloqueios: desconhecido — precisa ser medido nos logs do runner real.

**Gate proposto:** bloquear entrada AR quando `market_range_15s >= 0.03`.

### 3.4 Gate adicional: velocidade negativa
Antes das quedas mais graves (drop >= 0.20), investigar se `market_delta_15s < -0.01` já sinalizava queda antes da entrada. **Ainda não testado.**

---

## 4. Filtros implementados em `analyze_monitor_pnl.py`

| Filtro | Descrição | Efeito (164 trades, 13.8h) |
|---|---|---|
| Baseline | Sem filtro, AR normal | WR 62%, PnL +$12,00 |
| EXC.OPOSTO | Skip quando EL contradiz AR side | WR 62%, PnL **+$14,76** |
| F2+F3 | EL confirma + continuidade | WR 62%, avg **+$0,124** |
| F2+F3+F4 | Todos os filtros + spot_ok | WR 64%, avg **+$0,130** |

**EXC.OPOSTO é o melhor filtro de PnL absoluto** — remove 25 trades ruins, não exige novo sinal.

---

## 5. Testes Pendentes nos Logs do Runner Real

> **Execute esses testes após `git pull` no PC do runner real.**  
> Scripts disponíveis: `analyze_early_leader_all_logs.py`, `analyze_entry_threshold_and_gaps.py`, `analyze_monitor_pnl.py`

### TESTE A — Early Leader no runner real
```bash
python analyze_early_leader_all_logs.py --log-dir logs/
```
**Perguntas:**
- O EL tem o mesmo poder preditivo (79%+ baseline, 94% com F3)?
- A proporção de inversões de EL é similar (38+62 = 100 de 545)?
- Os logs do runner têm cobertura de secs 240+ em todos os mercados?

### TESTE B — EXC.OPOSTO em dados reais
```bash
python analyze_monitor_pnl.py --all-logs
```
**Perguntas:**
- O filtro EXC.OPOSTO melhora PnL nos logs reais?
- Com dados reais (execuções efetivas), o EL estável eleva o WR?

### TESTE C — Gate range_15s
```bash
python analyze_entry_threshold_and_gaps.py --drop-min 0.08
```
**Perguntas:**
- Em logs reais, qual % das perdas aconteceu com range_15s >= 0.03?
- Qual % dos trades totais seria bloqueado pelo gate (custo do filtro)?
- O gate muda o PnL positivamente após aplicado?

### TESTE D — EL stable + sem stop (hold to resolution precoce)
**Ainda sem script específico.**  
Hipótese: quando EL estável detectado em secs 200 e bid do EL >= 0.82, entrar sem stop (ou stop muito largo, 10T+), hold to resolution.  
Requer novo script: `analyze_el_hold_no_stop.py`

### TESTE E — Inversão de EL como sinal direcional
**Ainda sem script específico.**  
Hipótese: quando EL inverte em secs 180-121 com bid 0.60-0.72, comprar o novo líder (oposto ao EL original), stop 3T, hold to resolution.  
Requer novo script: `analyze_el_flip_signal.py`

---

## 6. Roadmap de Implementação

```
FASE 1 (pronto para teste aqui):
  [x] analyze_early_leader_all_logs.py   — análise de EL em todos os logs
  [x] analyze_entry_threshold_and_gaps.py — threshold + gaps
  [x] analyze_monitor_pnl.py com --all-logs e EXC.OPOSTO

FASE 2 (implementar após validar nos logs reais):
  [ ] Filtro EXC.OPOSTO no runner
      → Em run_market_monitor.py: detectar early_leader em secs 240-181,
        gravar no log, usar como gate para bloquear AR contraditório
  [ ] Gate range_15s no runner
      → Bloquear entrada quando market_range_15s >= 0.03 no snap de entrada

FASE 3 (novos setups — após validação com dados reais):
  [ ] EL stable + hold to resolution sem stop (análise pendente TESTE D)
  [ ] EL flip signal — comprar inversão de EL (análise pendente TESTE E)
  [ ] EL como sinal de entrada antecipada em secs 180-121

FASE 4 (runner de teste aqui):
  [ ] Implementar FASE 2 em run_market_monitor.py
  [ ] Rodar monitor por 24h+ e validar com analyze_monitor_pnl.py
```

---

## 7. Parâmetros de Referência

| Parâmetro | Valor atual | Observação |
|---|---|---|
| entry_min | 0.88 | Não alterar sem validação D |
| stop_ticks | 2 | Para entradas < 0.86, investigar stop mais largo |
| early_min | 0.55 | Threshold para detectar EL em secs 240-181 |
| cont_min | 0.70 | Threshold de continuidade do EL em secs 180-121 |
| range_15s gate | 0.03 | Proposto; validar nos logs reais (TESTE C) |
| qty | 6 shares | Não alterar |
