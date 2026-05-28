# Análise de Regime de Mercado — AR paper vs EE paper

**Data da análise:** 2026-05-28  
**Dados:** 838 trades AR paper + 216 trades EE paper (87 sessões)  
**Script:** `_analise_condicoes_mercado.py`

---

## AR paper — condicionantes do sucesso (WR 95.9%)

### Features por outcome (804 wins / 23 losses / 11 flat)

| Feature | WINS | LOSSES |
|---------|------|--------|
| `abs_distance_bps` | avg 18.1 | avg **13.9** |
| `passive_score_side` | avg 61.6 | avg **49.6** |
| `market_range_60s` | avg 0.10 | avg **0.14** |
| `secs_to_end` | avg 56s | avg 71s |

**Interpretação:** losses concentram-se quando o mercado ainda não se distanciou o suficiente do preço de abertura (`dist_bps < 14`) E o range dos últimos 60s está agitado (`range60 > 0.15`). O sinal é válido tecnicamente mas o preço ainda está em movimento — não consolidado.

### WR por variante

| Variante | Trades | WR | PnL ticks |
|---------|--------|-----|-----------|
| `extreme_99_limit` | 19 | **100%** | +144 |
| `dual_rich_late_limit` | 266 | **98.5%** | +527 |
| `standard` | 552 | 94.6% | +786 |
| `standard_reentry_pp_nostop` | 106 | 93.4% | +149 |

Variantes `extreme_99_limit` e `dual_rich_late_limit` entram quando o livro está >0.97 de um lado — reversão praticamente impossível, daí o WR próximo de 100%.

### Zona de risco AR (diagnóstico implementado)

Condição: `variant == "standard"` AND `abs_distance_bps < 14` AND `market_range_60s > 0.15`

- Detecta ~12 dos 23 losses históricos
- **NÃO é um gate** por ora — flag `risky_zone=true` no campo `regime_diagnostics` do evento `enter` para validar prospectivamente

### Detalhes dos losses (23 casos)

Todos no lado DOWN (exceto 2 UP em `dual_rich_late_limit` com timeout).  
Faixa de entry_price: 0.90–0.97.  
4 losses são reentradas pós-PP (`standard_reentry_pp_nostop`).

---

## EE paper — condicionantes do PnL negativo

### Distribuição de outcomes (216 trades totais)

| Outcome | Qty | % | PnL |
|---------|-----|---|-----|
| WIN | 30 | 13.9% | +$29.52 |
| PROFIT_PROTECT | 129 | 59.7% | +$87.06 |
| STOP_LOSS | 53 | 24.5% | **-$90.48** |
| REVERSAL | 2 | — | -$10.32 |
| WIN_HEDGE | 2 | — | -$6.78 |
| **Total** | 216 | — | **+$9.00** |

O EE no total geral é marginalmente positivo (+$0.04/trade). Dias negativos ocorrem por concentração de stops.

### Gate principal: n_s180

| n_s180 | Total | STOP | WR(W+PP) | PnL | Observação |
|--------|-------|------|---------|-----|-----------|
| 1 | 45 | 12 | 71.1% | +$2.94 | gate real: bloqueia |
| 2 | 31 | 11 | 64.5% | -$4.20 | gate real: bloqueia |
| 3 | 27 | 6 | 74.1% | -$0.36 | neutro |
| 4 | 12 | 3 | 66.7% | +$3.42 | neutro |
| **5** | **16** | **5** | **56.2%** | **-$4.68** | **gate candidato novo** |
| 6 | 15 | 2 | 80.0% | -$0.84 | aceitável |
| **7** | **13** | **0** | **92.3%** | **+$5.82** | **sweet spot** |
| 8 | 13 | 4 | 69.2% | -$0.06 | neutro |
| 9 | 5 | 0 | 80.0% | +$3.30 | positivo (n pequeno) |
| 10 | 16 | 4 | 75.0% | -$0.30 | neutro |
| 11 | 22 | 5 | 77.3% | +$3.30 | positivo |

**Conclusão:** `n_s180=5` é a pior faixa fora dos gates já deployados (WR 56.2%, PnL -$4.68). O "sweet spot" é n_s180=7 (0 stops, WR 92.3%).

### Gate horário UTC

| Horário | WR | Observação |
|---------|-----|-----------|
| **06h UTC** | **42.9%** | **gate candidato novo** |
| 20-21h UTC | 57-64% | zona de cuidado |
| 13h UTC | 50% | zona de cuidado (n pequeno) |
| 04h, 11h, 19h, 22h | 100% | melhor (amostras pequenas) |

### Gate por faixa de secs (entry_secs)

| secs | STOP | WR(W+PP) | Observação |
|------|------|---------|-----------|
| 30-60 | 1 | 91.7% | melhor faixa |
| 60-120 | 6 | ~80% | boa |
| 120-150 | 9 | 76.9% | aceitável |
| **150-181** | **37** | **72.2%** | maior volume de stops |

---

## Gates implementados em 2026-05-28 (paper)

### EE paper (`market/live_early_entry_paper_v1.py`)

Novos gates adicionados ao bloco `_regime_blocked`:

| Gate | Razão | Base empírica | Status |
|------|-------|--------------|--------|
| `n_s180 < 3` | Pouca convicção do EL | Já deployado no real (27/05) | Replicado no paper |
| `n_s180 == 5` | WR 56.2%, pior faixa pós-gate-real | 16 trades paper | Candidato — validar com 50+ dias úteis |
| `hora UTC == 6` | WR 42.9%, pior horário absoluto | 7 trades paper | Candidato — validar com mais dados |

Todos os bloqueios são logados como `entry_blocked` com `reason` específico para análise.

### AR paper (`diagnostics_current_almost_resolved_paper_v1.py`)

Campo `regime_diagnostics` adicionado ao evento `enter`:
- `risky_zone`: true quando `variant==standard` AND `dist_bps < 14` AND `range60 > 0.15`
- Não é um gate — apenas diagnóstico para validação prospectiva

---

## Quando operar cada setup

Os setups operam em fases diferentes do candle (EE: primeiros ~180s; AR: últimos ~95s) — não são mutuamente exclusivos.

| Cenário | EE | AR |
|---------|----|----|
| n_s180 >= 7 + horário 04-16h UTC | ✅ Alta confiança | ✅ Sempre bom |
| n_s180 < 3 | ❌ Bloqueado (gate real) | ✅ Independente |
| n_s180 = 5 | ❌ Bloqueado (gate paper candidato) | ✅ Independente |
| 06h UTC | ❌ Bloqueado (gate paper candidato) | ✅ Independente |
| 20-21h UTC | ⚠️ Monitorar | ⚠️ Se dist < 14bps + range60 > 0.15 → risky_zone |
| `standard` + dist < 14bps + range60 > 0.15 | n/a | ⚠️ risky_zone logado, aceita por ora |
| `dual_rich_late_limit` / `extreme_99_limit` | n/a | ✅ WR ~100%, sempre operar |

---

## Próximos passos

1. **Validar n_s180=5 e hora=6h** após 50+ dias úteis no paper com os novos gates ativos
2. **Validar zona de risco AR** após 50+ entradas com `risky_zone=true` no paper
3. **Puxar logs reais** do outro PC (`_run_real_analysis.ps1 -Push`) para confirmar se os gates do real estão comportando como esperado
4. **Gate n_s180=5 no runner real** — só após validação com WR > 70% e PnL positivo no paper
5. **Gate hora=6h no runner real** — validar primeiro se o horário é consistente ou sazonalidade de fim de semana
