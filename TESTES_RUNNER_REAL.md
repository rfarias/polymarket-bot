# Guia de testes nos logs do runner real

Scripts prontos para validar hipóteses no outro PC.
Rodar após `git pull` no PC com os logs reais.

---

## 1. Análise de variantes do almost_resolved

**Script:** `analyze_real_variants.py`

**O que faz:** Reconstrói cada trade real com as condições de entrada
(setup_variant, secs, entry_price, bid_decel_gate, counter_price) e testa
38 combinações de filtros para ver qual teria dado PnL >= 0.

```powershell
# Todos os logs reais na pasta logs/
python analyze_real_variants.py --log-dir logs/ --min-pnl 0

# Um diretório específico
python analyze_real_variants.py --log-dir logs/current_almost_resolved_real_20260601_120000

# Top 10 com PnL positivo
python analyze_real_variants.py --log-dir logs/ --min-pnl 0 --top 10
```

**O que olhar:**

| Coluna | Significado |
|--------|-------------|
| `Allow` | Trades que passariam o filtro |
| `Blk` | Trades bloqueados pelo filtro |
| `WR` | Win rate dos trades permitidos |
| `PnL` | PnL total dos trades permitidos |
| `ΔvsBase` | Ganho/perda vs não filtrar nada |

**Critério de sucesso:** variante com `PnL >= 0` e `Allow >= 10` trades.
Se PnL positivo só com 1-2 trades, desconsiderar (amostra insuficiente.

---

## 2. Hipótese de hedge parcial

**Script:** `analyze_hedge_hypothesis.py`

**O que faz:** Quando o mercado começa a reverter durante uma posição aberta,
simula comprar o lado contrário (loser) para limitar a perda. Testa 51
combinações de trigger × quantidade de hedge.

Triggers testados:
- **loser_rise**: loser_buy cruza acima de um threshold (0.04 a 0.20)
- **winner_drop**: winner_buy cai X bps do preço de entrada (1 a 10 bps)
- **early_insurance**: compra loser logo na entrada enquanto está barato (≤ 0.03/0.05/0.08)

```powershell
# Logs reais — mostrar só variantes que melhoram o PnL baseline
python analyze_hedge_hypothesis.py --log-dir logs/ --min-improvement 0

# Diretório específico
python analyze_hedge_hypothesis.py --log-dir logs/current_almost_resolved_real_20260601_120000 --min-improvement 0

# Incluir também os paper logs locais junto com os reais
python analyze_hedge_hypothesis.py --log-dir logs/

# Ajustar quantidade do hedge (padrão: 0.5x, 1.0x, 2.0x da posição original)
python analyze_hedge_hypothesis.py --log-dir logs/ --qty-ratios 0.5,1.0 --min-improvement 0
```

**O que olhar:**

| Coluna | Significado |
|--------|-------------|
| `PnL+Hedge` | PnL total com a estratégia de hedge |
| `Melhoria` | Diferença vs baseline sem hedge |
| `Hedged` | Quantas vezes o trigger disparou |
| `Salvou` | Trades perdedores que o hedge cobriu |
| `Custou` | Trades vencedores penalizados pelo hedge |
| `GanhoPerda` | PnL do hedge nos trades perdedores |
| `CustoWin` | Custo do hedge nos trades vencedores |

**Critério de sucesso:**
- `Melhoria > 0` com `Salvou >= 2` trades perdedores
- `Custou / Salvou` baixo (idealmente 0 — sem falso alarme nos wins)
- Trigger que disparou em losses mas não em wins = sinal confiável

**Resultado dos logs paper (referência):**

O trigger `loser_buy >= 0.06` disparou em 5/5 perdas e **zero vezes nas vitórias**.
Isso é o melhor caso possível. Verificar se o mesmo padrão aparece nos logs reais.

---

## 3. Se o hedge der resultado positivo → ir ao runner real

Antes de ligar o hedge no runner real, confirmar:

- [ ] Pelo menos 5 trades perdedores cobertos nos logs reais
- [ ] Custo nos wins é menor que o ganho nas perdas (GanhoPerda + CustoWin > 0)
- [ ] O trigger escolhido não dispara em mais de 20% dos trades vencedores
- [ ] Qty do hedge <= 1x a posição original para o primeiro teste real

O runner real a modificar é `market/live_current_almost_resolved_real_v1.py`.
A lógica de hedge entraria como um bloco novo quando `trade.mode == "open_position"`
detecta o trigger ativo no snapshot.

**Não implementar sem antes discutir com o Claude** — a modificação envolve
postar ordens reais no lado contrário, o que requer revisão de risk gates.

---

## Estrutura dos logs esperada

O runner real grava em:
```
logs/current_almost_resolved_real_YYYYMMDD_HHMMSS/
    current_almost_resolved_real.jsonl
```

Cada linha é um evento JSON. Os tipos relevantes para os scripts:
- `snapshot` — estado do mercado a cada poll (0.5s)
- `entry_filled` / `entry_confirmed` — confirmação de entrada
- `trade_summary` — resumo ao fechar posição
- `awaiting_redeem` — resolução final pelo mercado (side_won: true/false)
- `flat` / `redeem_flat` — saída intermediária

Se os arquivos estiverem em outro caminho, passar via `--log-dir`:
```powershell
python analyze_real_variants.py --log-dir "D:/polymarket/logs/"
python analyze_hedge_hypothesis.py --log-dir "D:/polymarket/logs/"
```
