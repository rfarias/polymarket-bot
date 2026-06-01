# Relatório de Valor Esperado (EV) — Abordagem Probabilística

**Atualizado:** 2026-06-01
**Versão anterior:** 2026-05-30

**Base acumulada:**
- AR paper: 1132 trades (26/05–01/06, 5 sessões × ~200 trades/dia)
- EE simulation: 199 trades combinados (RELATORIO v1 + sim 2529 slugs, vel≥0.17)
- win_prob real: 1368 slugs reais (seção 24 TESTES_ANALISE_EL.md)

## Metodologia

```
EV = WR_estatístico − preço_de_entrada
```

Pressupõe payoff hold-to-resolution: compra a P, recebe 1.0 no win, 0 no loss.
Se EV > 0, o mercado está precificando abaixo da probabilidade real de ganho.

---

## 1. AR Standard (Almost Resolved — Variant Standard)

**Base v1:** 897 trades (26–30/05)
**Base v2:** +235 trades (31/05–01/06, confirmação)
**Total:** 1132 trades

| Faixa de preço | n   | WR      | EV      | Sinal              |
|----------------|-----|---------|---------|--------------------|
| 0.90–0.91      | 48  |  87.5%  | −0.025  | ✗ Sem valor        |
| **0.91–0.92**  | 80  | **93.8%** | **+0.028** | ✓ EV+          |
| **0.92–0.93**  | 72  | **97.2%** | **+0.052** | ✓✓ Forte       |
| **0.93–0.94**  | 75  | **96.0%** | **+0.030** | ✓✓ Forte       |
| **0.94–0.95**  | 71  | **94.4%** | **+0.004** | ✓ Limite        |
| **0.95–0.96**  | 117 | **97.4%** | **+0.024** | ✓✓ EV+         |
| 0.96–0.97      | 182 |  95.1%  | −0.009  | ✗ Sem valor        |
| 0.97–0.98      | 158 |  93.0%  | −0.040  | ✗ Pior faixa AR    |
| **0.98–0.99**  | 232 | **99.1%** | **+0.011** | ✓ EV+ (dual)   |

> **Nota v2 (01/06):** amostras recentes (n=265) confirmam o padrão geral mas têm
> tamanhos de banda pequenos — usar tabela combinada acima como referência.

### Paradoxo estrutural

Pagar **0.97** por WR=93% é matematicamente pior do que pagar **0.92** por WR=97%.
O mercado superestima o risco nas faixas médias (0.92–0.96) e cobra prêmio excessivo
no "quase resolvido" (0.96+). Isso cria uma **zona de valor sistemática em 0.91–0.96**.

### Conclusão AR Standard

- **Zona de valor:** 0.91–0.96 (EV positivo)
- **Evitar:** abaixo de 0.91 (WR insuficiente) e acima de 0.96 (preço > WR)
- **Gate deployado:** `price <= 0.96` no runner real desde 2026-05-28 — correto

Losses do standard (acumulado): concentrados em 0.96–0.97 (maior grupo absoluto).
Exit reasons dos losses: structural_stop, timeout, stop (em ordem de frequência).

---

## 2. AR Dual Rich Late Limit

**Base v1:** 439 trades — WR 100%
**Base v2:** +120 trades (31/05–01/06, confirmação)
**Total: 559 trades — WR 100%**

| Preço | WR    | EV     | Veredicto             |
|-------|-------|--------|-----------------------|
| 0.98  | 100%  | +0.020 | ✓✓ Edge estrutural    |

Detecta estado de livro duplo rico perto da resolução. O mercado precifica 0.98
quando a probabilidade empírica é 1.0. Com 559 trades e WR 100%, é a entrada
**mais confiável e consistente** do portfólio.

Avg pnl: $1.00/trade (qty=50 shares). Não há sinal de degradação do edge.

---

## 3. EE (Early Entry) — por Velocidade e Preço de Entrada

**Base v1:** 93 trades (22/05–30/05, vel≥0.17)
**Base v2:** 106 trades (simulação 2529 slugs, vel≥0.17, sem gate spread)
**Total combinado: 199 trades**

> **Mudança v2 (01/06):** gate `spread≥0.70` removido do paper após simulação
> mostrar que com vel≥0.17 não filtra perdas (WR idêntica com/sem: 88.7%).
> Ver commit 7c0c0cd.

### Por velocidade (universo completo, vel variável)

| Gate vel      | n   | WR    | Avg pnl  | Veredicto                   |
|---------------|-----|-------|----------|-----------------------------|
| vel < 0.10    |  58 | 51.7% | −$0.62   | ✗ Destruição de capital     |
| vel 0.10–0.13 |  49 | 75.5% | −$0.01   | ✗ Breakeven negativo        |
| vel 0.13–0.17 |  51 | 76.5% | +$0.01   | ≈ Nulo                      |
| **vel 0.17–0.20** | 44 | **88.6%** | **+$0.51** | ✓ EV+               |
| **vel ≥ 0.20**    | 49 | **87.8%** | **+$0.53** | ✓ EV+               |

O gate `vel ≥ 0.17` é o divisor entre edge positivo e negativo.

### Por preço de entrada (vel ≥ 0.17, 199 trades combinados)

| ep   | n_v1 | n_v2 | n_total | WR comb. | EV       | Veredicto              |
|------|------|------|---------|----------|----------|------------------------|
| 0.82 |  25  |   8  |   33    |  81.8%   | −0.002   | ✗ Armadilha — evitar   |
| 0.83 |  19  |  24  |   43    |  83.7%   | +0.007   | ≈ Neutro               |
| **0.84** | 22 | 19 | **41** | **95.1%** | **+0.111** | ✓✓✓ **Melhor EV** |
| **0.85** | 20 | 23 | **43** | **90.7%** | **+0.057** | ✓✓ Forte       |
| **0.86** |  7  |  32  |   39    | **92.3%** | **+0.063** | ✓✓ Forte       |

### Insight ep=0.84

WR salta de 83.7% (ep=0.83) para 95.1% (ep=0.84) com EV=+0.111.
Uma entrada em 0.84 com vel alto indica que o EL está num patamar de aceleração
que o mercado ainda não absorveu no preço. É a **maior anomalia estatística
identificada** no portfólio — EV > 10 centavos por dólar investido.

### Por secs (vel ≥ 0.17, amostra v1)

| Secs entrada | n   | WR    | EV     | Veredicto     |
|--------------|-----|-------|--------|---------------|
| > 160        |  31 | 87.1% | +0.047 | ✓             |
| 120–160      |  26 | 88.5% | +0.045 | ✓             |
| 80–120       |  18 | 94.4% | +0.094 | ✓✓ Melhor     |
| 30–80        |  18 | 83.3% | +0.003 | ≈ Neutro      |

Entrar muito tarde (secs 30–80) reduz o EV — mercado já convergiu mais.

### Conclusão EE

- **Zona de valor:** vel ≥ 0.17 + ep 0.84–0.86
- **Melhor combinação:** ep=0.84 + vel≥0.17 + secs 80–160 → EV≈+0.10
- **ep=0.82 parece barato mas não tem edge:** WR=81.8% ≈ preço pago
- **Gate spread removido:** era redundante com vel≥0.17 (simulação 2529 slugs)

---

## 4. EL Reversal (opp_bid ≥ 0.80)

**Base:** 398 slugs com opp_max ≥ 0.80 nos 1368 slugs reais — WR reversal = 94%

| Preço disponível (opp_bid) | n snaps | EV (WR=94%) | Veredicto        |
|---------------------------|---------|-------------|------------------|
| **~0.80**                 |  27     | **+0.140**  | ✓✓✓ Maior EV    |
| **~0.85**                 |  44     | **+0.090**  | ✓✓               |
| **~0.90**                 |  33     | **+0.040**  | ✓                |
| ~0.95                     |  32     | −0.010      | ✗ Sem valor      |
| ~1.00                     |  45     | −0.060      | ✗ Já resolvido   |

Condições de entrada: opp ≥ 0.80 + gap ≥ 0.35 + secs > 35.
Frequência muito baixa (EL Flip paper: 0 trades em 22h de coleta).
Quando ocorre, é o maior EV individual do portfólio.

---

## 5. Sem Valor — O Que NÃO Fazer

| Setup                    | WR    | Preço típico | EV      | Problema              |
|--------------------------|-------|--------------|---------|-----------------------|
| AR standard 0.96–0.97    | 95.1% | 0.97         | −0.009  | Preço > WR            |
| AR standard 0.97–0.98    | 93.0% | 0.97         | −0.040  | Pior trade do portfólio |
| AR standard ≤ 0.90       | 87.5% | 0.90         | −0.025  | WR insuficiente       |
| EE ep=0.82               | 81.8% | 0.82         | −0.002  | Parece barato; não é  |
| EE vel < 0.13            | 51–76%| 0.82–0.86    | <−0.06  | WR destruída          |
| EL Reversal opp ≥ 0.95   | 94%   | 0.95+        | −0.010  | Preço ≥ WR            |
| EL base (sem filtro vel) | 70%   | qualquer     | <0      | Mercado precifica bem |

---

## 6. Ranking de Oportunidades (atualizado 01/06)

| # | Setup                           | WR     | Preço ideal | EV       | n base | Confiança         |
|---|---------------------------------|--------|-------------|----------|--------|-------------------|
| 1 | EE vel≥0.17 @ ep=0.84          | 95.1%  | 0.84        | **+0.111** | 41   | Média             |
| 2 | EL Reversal opp 0.80            | 94%    | 0.80        | **+0.140** | 27 snaps | Baixa (raro) |
| 3 | **AR std 0.92–0.93**            | 97.2%  | 0.92        | **+0.052** | 72   | Alta              |
| 4 | EE vel≥0.17 @ ep=0.86          | 92.3%  | 0.86        | **+0.063** | 39   | Média             |
| 5 | EE vel≥0.17 @ ep=0.85          | 90.7%  | 0.85        | **+0.057** | 43   | Média             |
| 6 | **AR std 0.93–0.94**            | 96.0%  | 0.93        | **+0.030** | 75   | Alta              |
| 7 | **AR std 0.91–0.92**            | 93.8%  | 0.91        | **+0.028** | 80   | Alta              |
| 8 | **AR dual_rich @ 0.98**         | 100%   | 0.98        | **+0.020** | 559  | Muito alta        |
| 9 | AR std 0.95–0.96                | 97.4%  | 0.95        | +0.024   | 117  | Alta              |
| 10 | EL Reversal opp 0.85           | 94%    | 0.85        | +0.090   | 44 snaps | Baixa (raro) |

> **Leitura do ranking:** o EV unitário não é tudo — confiança estatística importa.
> O dual_rich (#8) tem EV modesto mas é a aposta mais segura do portfólio (n=559).
> O EE@0.84 (#1) tem o maior EV mas n=41, ainda requer validação no runner real.

---

## 7. Próximos Passos (atualizado 01/06)

1. **Runner real (imediato):**
   - Gate `price ≤ 0.96` já ativo — correto, não alterar
   - Foco em 0.91–0.96 standard + dual_rich: setups #3, #6, #7, #8, #9 são capturados
   - Confirmar via logs reais que entradas em 0.97+ estão sendo bloqueadas

2. **EE paper local (em progresso):**
   - vel≥0.17 ativo; gate spread removido em 01/06 (commit 7c0c0cd)
   - Objetivo: acumular 50+ dias úteis de trades para confirmar EV por ep
   - Prioridade: confirmar anomalia ep=0.84 (EV=+0.111) com n≥100
   - Não subir para runner real sem essa base

3. **EE vel no runner real:**
   - Atual: vel≥0.13 (inclui zona EV≈0 de vel 0.13–0.17)
   - Candidato: subir para vel≥0.17 — aguarda validação paper (50+ dias úteis)

4. **EL Reversal:**
   - Maior EV unitário (+0.14) mas rarísssimo
   - EL Flip paper rodando; aguardar acúmulo de trades antes de qualquer gate

5. **Não fazer:**
   - Não comprar AR standard ≥ 0.97 (EV negativo confirmado em 1132 trades)
   - Não entrar EE com vel<0.13 (WR destrói capital)
   - Não expandir qty sem validação nas faixas corretas de preço

---

*v1: 2026-05-30 — base 897 AR + 252 EE + 1368 slugs reais*
*v2: 2026-06-01 — base 1132 AR + 199 EE + simulação 2529 slugs; gate spread EE removido*
