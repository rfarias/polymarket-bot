# Relatório de Valor Esperado (EV) — Abordagem Probabilística

**Data:** 2026-05-30  
**Base de dados:** Papers locais 26–30/05 + análise win_prob 1368 slugs reais (seção 24 TESTES_ANALISE_EL.md)

## Metodologia

`EV = WR_estatístico − preço_de_entrada`

Se EV > 0, a cotação de mercado está abaixo da probabilidade real de ganho — é onde existe vantagem matemática. Pressupõe payoff hold-to-resolution (compra a P, recebe 1.0 no win, 0 no loss).

---

## 1. AR Standard (Almost Resolved — Variant Standard)

**Base:** 897 trades | Paper 26–30/05

| Faixa de preço | n | WR empírico | EV     | Sinal          |
|----------------|---|-------------|--------|----------------|
| 0.90–0.91      | 37  | 86.5% | −0.040 | ✗ Sem valor    |
| **0.91–0.92**  | 61  | **93.4%** | **+0.019** | ✓ EV+     |
| **0.92–0.93**  | 61  | **98.4%** | **+0.059** | ✓✓ Melhor faixa |
| **0.93–0.94**  | 58  | **96.6%** | **+0.031** | ✓✓ EV+ forte  |
| **0.94–0.95**  | 55  | **94.5%** | **+0.001** | ✓ Limite       |
| **0.95–0.96**  | 92  | **96.7%** | **+0.012** | ✓ EV+          |
| 0.96–0.97      | 137 | 94.9% | −0.016 | ✗ Sem valor    |
| 0.97–0.98      | 158 | 93.0% | −0.045 | ✗ Pior faixa   |
| **0.98–0.99**  | 232 | **99.1%** | **+0.006** | ✓ EV+      |

### Conclusão

O mercado é paradoxalmente mais caro em 0.96–0.97 do que em 0.92–0.95.  
Pagar 0.97 por WR=93% é pior do que pagar 0.93 por WR=96%.  
**Não comprar AR standard acima de 0.96.**

Losses do standard (38 no total): concentrados em 0.96–0.97 (18 losses = 47% dos losses).  
Exit reasons dos losses: structural_stop (16), timeout (12), stop (10).

---

## 2. AR Dual Rich Late Limit

**Base:** 439 trades — WR 100% — preço fixo 0.98 — todos saem via `target`

| Preço | WR    | EV     | Veredicto           |
|-------|-------|--------|---------------------|
| 0.98  | 100%  | +0.020 | ✓✓ Edge estrutural  |

Detecta estado de livro duplo rico perto da resolução.  
Com 439 trades e WR 100%, é a entrada mais confiável do portfólio.  
Avg pnl $1.00/trade (qty=50 shares).

---

## 3. EE (Early Entry) — Gate por Velocidade

**Base:** 252 trades totais (22/05–30/05)

| Gate vel    | n  | WR    | Avg pnl | Veredicto                  |
|-------------|----|-------|---------|----------------------------|
| vel < 0.10  | 58 | 51.7% | −$0.62  | ✗ Destruição de capital    |
| vel 0.10–0.13 | 49 | 75.5% | −$0.01 | ✗ Breakeven negativo      |
| vel 0.13–0.17 | 51 | 76.5% | +$0.01 | ≈ Nulo                    |
| **vel 0.17–0.20** | 44 | **88.6%** | **+$0.51** | ✓ EV+        |
| **vel ≥ 0.20**    | 49 | **87.8%** | **+$0.53** | ✓ EV+        |

### Com vel ≥ 0.17 (93 trades, WR 88.2%, avg $0.517)

| Entry price | n  | WR    | EV (hold-to-res) | Veredicto |
|-------------|-----|-------|-----------------|-----------|
| **0.82**    | 25  | 84.0% | **+0.020**      | ✓         |
| **0.83**    | 19  | 84.2% | **+0.012**      | ✓         |
| **0.84**    | 22  | 95.5% | **+0.115**      | ✓✓        |
| **0.85**    | 20  | 90.0% | **+0.050**      | ✓         |
| 0.86        |  7  | 85.7% | −0.003          | ≈ Limite  |

### Conclusão

O gate atual no runner real (`vel ≥ 0.13`) inclui uma zona de WR 76% quase nula.  
**Subir para `vel ≥ 0.17` é o próximo gate candidato para EE** — mas requer 50+ dias úteis de validação no paper antes de ir ao runner real.

Outcomes breakdown (vel ≥ 0.17): PROFIT_PROTECT (69), WIN (13), STOP_LOSS (11). 
Nota: paper ainda usa PP a 0.88; runner real com PP removido (commit cb44bcd) terá WR diferente.

---

## 4. EL Reversal (opp_bid ≥ 0.80)

**Base:** 398 slugs com opp_max ≥ 0.80 nos 1368 slugs reais — WR reversal = 94%  
(Seção 24.3 TESTES_ANALISE_EL.md)

Distribuição de preços disponíveis no mercado (market_monitor 29/05, 181 snaps com opp ≥ 0.80):

| Preço disponível (opp_bid) | n snaps | EV (WR=94%) | Veredicto       |
|---------------------------|---------|-------------|-----------------|
| **~0.80**                 | 27 (15%)| **+0.14**   | ✓✓ Melhor EV    |
| **~0.85**                 | 44 (24%)| **+0.09**   | ✓✓              |
| **~0.90**                 | 33 (18%)| **+0.04**   | ✓               |
| 0.95                      | 32 (18%)| −0.01       | ✗ Sem valor     |
| 1.00                      | 45 (25%)| −0.06       | ✗ Já resolvido  |

### Conclusão

EV real em opp_bid 0.80–0.90 (+0.04 a +0.14). Porém as condições de entrada  
(opp ≥ 0.80 + gap ≥ 0.35 + secs > 35) são raras — EL Flip paper rodou 22h e teve 0 trades.  
Quando ocorre, é o maior EV individual do portfólio.

---

## 5. Sem Valor — O Que NÃO Fazer

| Setup                   | WR    | Preço típico | Problema              |
|-------------------------|-------|--------------|-----------------------|
| AR standard 0.96–0.97   | 93–95%| 0.96–0.97    | Preço > WR → EV−      |
| AR standard 0.90        | 86.5% | 0.90         | WR insuficiente       |
| EE vel < 0.13           | 51–76%| 0.82–0.86    | WR muito baixa        |
| EL base (sem filtro)    | 70%   | qualquer     | Mercado precifica bem |
| EL reversal opp ≥ 0.95  | 94%   | 0.95–1.00    | Preço ≥ WR            |

---

## 6. Ranking de Oportunidades

| # | Setup                          | WR    | Preço ideal | EV         | Confiança         |
|---|--------------------------------|-------|-------------|------------|-------------------|
| 1 | AR dual_rich_late_limit        | 100%  | 0.98        | +0.020     | Alta (n=439)      |
| 2 | AR standard 0.92–0.93          | 98.4% | 0.92        | +0.059     | Alta (n=61)       |
| 3 | AR standard 0.93–0.94          | 96.6% | 0.93        | +0.031     | Alta (n=58)       |
| 4 | EL Reversal opp 0.80–0.85      | 94%   | 0.80–0.85   | +0.09–0.14 | Média (n=78 slugs)|
| 5 | EE vel ≥ 0.17 @ ep=0.84        | 95.5% | 0.84        | +0.115     | Baixa (n=22)      |
| 6 | AR standard 0.95–0.96          | 96.7% | 0.95        | +0.012     | Alta (n=92)       |
| 7 | EE vel ≥ 0.17 @ ep=0.82–0.83   | 84%   | 0.82–0.83   | +0.015     | Baixa (n=44)      |

---

## 7. Próximos Passos

1. **Imediato (runner real):** Os setups #1, #2, #3 têm base estatística sólida e são candidatos diretos. O AR real já captura esses cenários — verificar se o runner está selecionando preços de entrada na faixa 0.91–0.96 e não comprando a 0.97+.

2. **Gate EE vel ≥ 0.17:** Subir de 0.13 para 0.17 no paper. Aguardar 50+ dias úteis de dados antes de ir ao runner real. Não alterar o gate do runner real sem essa validação.

3. **EL Reversal:** Manter o EL Flip paper rodando para acumular amostras reais. A estratégia tem o maior EV unitário, mas frequência muito baixa (0 trades em 22h).

4. **Não fazer:** Não comprar AR standard em 0.96–0.97. Mesmo com WR alta, o preço está acima da probabilidade de ganho.

---

*Análise gerada em 2026-05-30. Dados: papers locais 26–30/05 + 1368 slugs reais (seção 24).*
