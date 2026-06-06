# EV Scanner — Resultados e Análise (2026-06-06)

## Resultados reais acumulados (modelo correto, desde 05/jun)

| Setup | n resolvidas | W | L | WR | PnL simulado |
|-------|-------------|---|---|----|-------------|
| `weather` | 15 | 2 | 13 | 13.3% | +$64.23 |
| `nba_nfl` | 2 | 1 | 1 | 50.0% | +$19.70 |
| **TOTAL** | **17** | **3** | **14** | **17.6%** | **+$83.93** |

Capital total apostado (17 × $20): $340  
ROI realizado: **+24.7%**

Entradas ainda abertas: 934 (mercados futuros NBA + temperature próximos dias)

---

## Por que o resultado anterior era negativo

Os logs de 02–04/jun foram arquivados como "contaminados" — gerados por um modelo
weather com prior circular (usava o próprio preço da Polymarket como estimativa de
probabilidade real, o que nunca gerava edge real). Commit de correção: `8dd9ceb`.

---

## Análise do grande vencedor — Hong Kong 30°C (+$246.67)

**Trade:** entrada a 7.5% (20/0.075 = 266.7 shares), resolved YES a 0.998 → pnl = +$246.67

**Por que o mercado estava errado:**  
A previsão do tempo apontava ~31°C. O mercado concentrou probabilidade em 31°C
(poly=15–28%) e deixou 30°C sub-precificado (poly=7.5%). A frequência histórica
climatológica real para 30°C em HK em junho é **18.7%** (300 amostras, 2006–2025).  
Com poly=7.5% e clim=18.7%, edge=11.2% — trade legítimo.

**Fundamental ou sorte?** Ambos.  
- Fundamental: ancoragem do mercado no ponto focal de previsão cria edge sistemático
  em buckets adjacentes.  
- Sorte: era um evento de ~19% que aconteceu desta vez.

---

## Instabilidade do clim_prob — causa e correção (commit `7f99f38`)

**Problema identificado:** `_fetch_climatology` fazia 4 chamadas à API em batches de
5 anos. Qualquer falha era silenciosa (`except: pass`), gerando distribuições parciais.
Para HK jun6 o prob_30C variava de **0.12 a 0.38** dependendo de quais batches chegavam.

| Batches disponíveis | n amostras | prob_30C (HK jun6) |
|--------------------|-----------|-------------------|
| Só 2011–2015 | 75 | **0.347** |
| 2006+2011 | 150 | 0.253 |
| 2006+2011+2016 | 225 | 0.209 |
| **Todos os 4 batches** | **300** | **0.187** ✓ |
| Só 2016–2021 | 150 | 0.120 |

**Fixes aplicados (`ev_scanner/setups/weather.py`):**
1. Batch falha → retorna `None` imediatamente (sem distribuição parcial)
2. Cache persiste em `ev_scanner/logs/clim_cache.json` entre sessões
3. Campo `clim_samples` nos logs agora registra n total de dias históricos (era nº de graus distintos)

---

## Simulação counterfactual — e se usássemos a config atual desde 02/jun?

Com as configurações atuais (weather model correto, fed_rate=false, soccer=false,
dedup NBA correto, min_price=0.05, edge>=0.08) aplicadas desde 02/jun:

| Data entrada | Setup | Mercado | Bucket/Outcome | poly | clim | edge | Resultado |
|-------------|-------|---------|----------------|------|------|------|-----------|
| 2026-06-04 | nba_nfl | NYK-SAS | Knicks | 0.335 | — | 0.239 | **WIN +$39.70** |
| 2026-06-04 | nba_nfl | NYK-SAS | Spurs | 0.085 | — | 0.341 | LOSS -$20.00 |
| 2026-06-05 | weather | Tokyo 05/jun | 23°C | 0.070 | 0.150 | 0.080 | LOSS -$20.00 |
| 2026-06-05 | weather | HK 06/jun | 30°C | 0.090 | 0.187 | 0.097 | **WIN +$202.22** |
| 2026-06-06 | weather | Tokyo 06/jun | 24°C | 0.050 | 0.240 | 0.190 | LOSS -$20.00 |
| 2026-06-06 | weather | Tokyo 07/jun | 24°C | 0.095 | 0.237 | 0.142 | ABERTA |
| 2026-06-06 | weather | HK 06/jun | 31°C | 0.060 | 0.140 | 0.080 | LOSS -$20.00 |
| 2026-06-06 | weather | SP 06/jun | 24°C | 0.065 | 0.147 | 0.082 | ABERTA |
| 2026-06-06 | weather | HK 07/jun | 28°C | 0.085 | 0.223 | 0.138 | ABERTA |

**Resolvidas:** 6/9 → W=2 L=4 → **PnL = +$161.92**  
**Capital em aberto:** 3 × $20 = $60

### Principais diferenças vs resultado real

| Item | Modelo antigo | Config atual | Impacto |
|------|--------------|--------------|---------|
| NBA NYK-SAS | 193 entradas (bug dedup) | 2 entradas | Evitou 191 duplicatas |
| Weather arquivo | 131 entradas (modelo circular) | 1 entrada | Eliminou edge falso |
| Fed_rate | 113 entradas (ativo) | 0 (desativado) | ~$2.260 capital liberado |
| HK 30°C win | Entrou a 0.075 → +$246.67 | Teria entrado a 0.090 → +$202.22 | Dedup: entra mais cedo a preço pior |

### Lição do dedup no weather

O scanner com dedup correto teria entrado HK jun6 30°C **mais cedo** (04/jun,
poly=0.09) em vez de esperar até 05/jun (poly=0.075). Entrar mais cedo significa
preço maior → menos shares → payout menor (+$202 vs +$247). O mercado foi ficando
mais barato conforme a data se aproximava. Estratégia ótima: entrar o mais tarde
possível enquanto ainda há edge — mas é difícil de saber sem previsão de quanto o
preço vai cair.

---

## Próximos passos

1. Aguardar resolução das 3 entradas abertas (Tokyo 07/jun, SP 06/jun, HK 07/jun)
2. Monitorar NBA: jogos com edge detectado (dedup correto desde `c9c8210`)
3. Após 50+ resoluções weather: avaliar se WR empírica (13.3% atual) está acima do
   preço médio de entrada (8–10%) de forma consistente
4. Avaliar se `min_price=0.05` está certo: muitos mercados com edge real têm
   poly < 0.05 (ex: HK 28°C or below: poly=0.005, edge=0.43) — mas liquidez
   provavelmente é zero nessas faixas
