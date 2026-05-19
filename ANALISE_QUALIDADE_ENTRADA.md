# Análise de Qualidade de Entrada — Documentação

**Data:** 2026-05-19  
**Status:** Validado nos logs reais (48 trades). Gate `entry_too_late` implementado em produção (commit `985db6e`).

---

## O Problema

96%+ win rate em paper, mas **-700 USD real** (conta rfarias) por causa de assimetria de payoff:
- Wins reais: +5 a +15 USD por trade
- Losses reais: -200 a -500 USD (ou perda total quando stop não preenche)

A causa: quando o mercado reverte próximo da resolução, o book Polymarket some. Stop em 0.93 pode preencher a 0.57 — ou não preencher. Paper assume fill exato no stop_price, o que é falso.

---

## A Hipótese Principal

**`exit_dist`** = distância entre preço de entrada e target (ex: entrada 0.98, target 0.99 → `exit_dist = 0.01`).

Validado em 289 trades paper do setup `current_almost_resolved`:

| Segmento | Trades | WR | Pior perda em paper |
|---|---|---|---|
| `exit_dist < 0.02` (1 tick do target) | 144T | **100%** | **zero losses** |
| `exit_dist >= 0.02` standard | 139T | 88% | -13 ticks |

**Todos os losses estão no segmento `exit_dist >= 0.02`.**  
O segmento `exit_dist < 0.02` tem 144 trades, 100% WR, zero losses.

### Por que isso faz sentido

- `exit_dist = 0.01`: entrada em 0.98, target 0.99. O market já está quase resolvido. Para perder, precisa cair mais de 3 ticks até o stop. Mesmo em livro thin, o stop em 0.95 provavelmente tem liquidez.
- `exit_dist = 0.05`: entrada em 0.94, target 0.99. Precisa subir 5 ticks. Se cair 5 ticks, stop em 0.91 — mas se o book summer, fill pode ser 0.70 ou zero.

---

## Filtro Tri-nível Proposto

```
Tier 1: exit_dist < 0.02         → 100% WR, zero losses, ENTRAR
Tier 2: controlled_late_entry
        resolved_pullback_limit   → 100% WR, ENTRAR
Tier 3: standard + exit_dist < 0.04
        + range15s == 0
        + entry >= 0.97           → 96% WR, pior -1 tick, ENTRAR com cautela
BLOCK:  resto                     → 87-88% WR, contém todos os losses grandes
```

Resultado no paper:

| | Trades | WR | PnL total | Pior |
|---|---|---|---|---|
| PERMITIDO (T1+T2+T3) | 186/289 | **99%** | +328tk | -1 tk |
| BLOQUEADO | 103/289 | 87% | +208tk | -13 tk |

---

## Como Validar nos Logs Reais (PC de Casa)

### Passo 1: Atualizar

```powershell
git pull
git log --oneline -5
# Deve aparecer o commit com analyze_entry_quality_on_logs.py
```

### Passo 2: Rodar o script nos logs reais

O script detecta automaticamente as sessões do runner em `logs/current_almost_resolved_real_*/`:

```powershell
python analyze_entry_quality_on_logs.py
```

Se quiser um diretório específico:
```powershell
python analyze_entry_quality_on_logs.py --dir logs\current_almost_resolved_real_20260519_120000
```

### Passo 3: Interpretar o output

O script vai mostrar:

```
TIER                                            N    WR     PnL    avg   pior
------------------------------------------------------------------------
Tier 1 — exit_dist<0.02  (1 tick do target)   ??  ???%   +???  +?      ???
Tier 2 — controlled_late / resolved_pullback   ??  ???%   +???  +?      ???
Tier 3 — standard, calm market, entry>=0.97    ??  ???%   +???  +?      ???
BLOCK   — standard, distante ou volátil         ??  ???%   +???  +?      ???
```

**O que procurar:**
1. Tier 1 tem WR alto e pior loss pequeno? → hipótese confirmada
2. BLOCK tem losses maiores que Tier 1? → filtro funciona
3. Algum loss em Tier 1? → precisa revisar o filtro
4. `AVISO: exit_dist=0` aparece? → campo não está no sinal desta versão

### Passo 4: Se `exit_dist` não estiver populado

O campo `up_exit_distance` / `down_exit_distance` existe no sinal desde pelo menos 2026-04-27. Se o aviso aparecer, verificar:

```powershell
# Buscar se o campo existe nos logs
Select-String -Path logs\current_almost_resolved_real_*\*.jsonl -Pattern "exit_distance" | Select-Object -First 5
```

Se não existir, o script vai mostrar todos os trades em Tier 1 (por default exit_dist=0 < 0.02), o que invalida a análise.

---

## Resultado da Validação nos Logs Reais (2026-05-19)

### Hipótese original vs dados reais

A hipótese exit_dist (paper) mostrou padrão diferente nos logs reais antes da correção de PnL:

**Problema identificado:** `analyze_entry_quality_on_logs.py` calculava PnL usando `exit_price_posted`
para ordens FAK de `near_win_exit` e `oracle_margin_exit`. O fill real é ao preço do maker (bid),
não ao limite postado. Ex: posted=0.99, bid=1.0 → script calculava pnl=0 (loss), real=+1 tick (win).

**Correção aplicada** (commit `5b9722a`): extrai bid de `last_reason` para esses tipos de exit.

### Resultados corrigidos (48 trades reais)

| Tier | N | WR | PnL |
|------|---|----|-----|
| Tier 1 (exit_dist<0.02) | 20 | 90% | -88tk |
| Tier 2 (controlled_late/resolved_pullback) | 4 | 100% | +10tk |
| BLOCK | 24 | 83% | +5tk |

**Por que Tier 1 tem PnL negativo mesmo com 90% WR:** dois outliers no Tier 1 pesam muito:
- `-97.9tk` standard (BTC reverteu 30+ bps em 83s — não filtrável na entrada)
- `-12.0tk` dual_rich_late_limit (secs=29 → book collapse → **filtrado pelo novo gate**)

### 6 Perdas ≥ 5 ticks analisadas

| PnL | Variant | Secs | Range15s | Dist_ptb | Entry | Causa | Filtrável? |
|-----|---------|------|---------|---------|-------|-------|-----------|
| -97.9tk | standard | 94 | 0.000 | 30.5 | 0.98 | BTC -30bps em 83s (extremo) | Não |
| -17.0tk | standard | 27 | 0.020 | 5.3 | 0.96 | Book collapse secs<30 | **Sim — entry_too_late** |
| -12.0tk | standard | 94 | 0.010 | 9.3 | 0.97 | dist_ptb baixo + stop GTC slippage | Não (custo > benefício) |
| -12.0tk | dual_rich | 29 | 0.000 | 17.6 | 0.98 | Book collapse secs<30 | **Sim — entry_too_late** |
| -12.0tk | standard | 95 | 0.000 | 20.1 | 0.96 | Reversão real, entry longe | Não (custo > benefício) |
| -5.0tk | standard | 62 | 0.000 | 10.3 | 0.93 | Reversão real, entry longe | Não (custo > benefício) |

### Análise de filtros candidatos

| Filtro | blk_L | blk_W | Ticks salvos | Ticks perdidos | Net |
|--------|-------|-------|-------------|----------------|-----|
| secs<30 (standard+dual_rich) | 2 | 3 | +29.0tk | +2.1tk | **+26.9tk** |
| dist_ptb_bps < 10 | 2 | 15 | +29.0tk | +31.1tk | -2.1tk |
| entry_price ≤ 0.96 | 3 | 18 | +34.0tk | +49.9tk | -15.9tk |

**Único filtro cirúrgico com retorno positivo:** `secs < 30` para `standard` e `dual_rich_late_limit`.

---

## Gate Implementado: `entry_too_late`

**Commit:** `985db6e` | **Arquivo:** `market/live_current_almost_resolved_real_v1.py` (linha ~1311)

```python
if (
    str(signal.get("setup_variant") or "") in ("standard", "dual_rich_late_limit")
    and current_secs is not None
    and current_secs < 30
):
    _append_jsonl(log_path, {
        "type": "entry_blocked",
        "ts": now,
        "session_id": session_id,
        "reason": f"entry_too_late:secs={current_secs}",
        "signal": signal,
    })
    time.sleep(poll_secs)
    continue
```

**Isentos por design:** `controlled_late_entry`, `resolved_pullback_limit`, `passive_extreme_liquidity_capture` (tem gate próprio).

**Por que não implementar o filtro tri-nível (exit_dist)?**
Após validação nos logs reais, a hipótese original (exit_dist<0.02 = zero losses) não se sustentou:
- Tier 1 tem 90% WR, BLOCK tem 83% — diferença pequena
- As perdas no Tier 1 têm causas específicas já tratadas (book collapse secs<30, BTC extremo)
- Bloquear BLOCK custaria 20 wins por 4 losses evitadas — resultado negativo

---

## Notas: Gate `entry_too_late` não é o mesmo que `entry_quality_gate`

O gate de qualidade de entrada tri-nível (exit_dist baseado) **não foi implementado** pois a
análise real mostrou que o problema principal é o book collapse nos últimos 30s, não a distância
ao target. O `entry_too_late` é mais cirúrgico e tem melhor custo-benefício documentado.

---

## Contexto: Gates Implementados (em ordem cronológica)

1. **leader_velocity_too_high** — bloqueia se mercado moveu muito rápido (existia antes)
2. **Cooldown de re-entrada 25s** (commit `7e13a66`) — bloqueia re-entrada imediata após exit
3. **passive_capture_too_late** (commit `2167209`) — bloqueia passive_extreme secs<30
4. **entry_too_late** (commit `985db6e`) — bloqueia standard + dual_rich secs<30

---

## Referência Rápida dos Scripts de Análise

| Script | O que faz |
|---|---|
| `analyze_entry_quality_on_logs.py` | **Este** — tiers de qualidade de entrada |
| `analyze_reentry_cooldown_on_logs.py` | Simula cooldown 25s nos logs |
| `analyze_chart_context_on_logs.py` | Chart context BTC (não preditivo para este setup) |
