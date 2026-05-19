# Análise de Qualidade de Entrada — Documentação

**Data:** 2026-05-19  
**Status:** Hipótese validada em paper (289 trades). Aguarda validação nos logs reais do PC de casa.

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

## O Que Não Está Implementado Ainda

O gate no runner real ainda **não foi implementado**. O filtro existe apenas na análise. 

Só implementar após confirmar nos logs reais que:
1. `exit_dist` está populado nos eventos `enter` do runner
2. Os losses reais (ou quase-losses) estão no segmento `exit_dist >= 0.02`
3. O segmento `exit_dist < 0.02` tem WR alto nos logs reais

**Quando confirmar**: implementar o gate em `market/live_current_almost_resolved_real_v1.py`:

```python
# No bloco de entrada idle (após os outros gates):
_exit_dist = _safe_float(signal.get("up_exit_distance" if side == "UP" else "down_exit_distance"))
_range15s  = _safe_float(signal.get("market_range_15s"))
_entry_p   = _safe_float(signal.get("entry_price"))
_variant   = str(signal.get("setup_variant") or "")

_tier1 = _exit_dist < 0.02
_tier2 = _variant in ("controlled_late_entry", "resolved_pullback_limit")
_tier3 = (_variant == "standard" and _exit_dist < 0.04 and _range15s == 0.0 and _entry_p >= 0.97)

if not (_tier1 or _tier2 or _tier3):
    _append_jsonl(log_path, {
        "type": "entry_blocked",
        "reason": "entry_quality_gate",
        "exit_dist": _exit_dist,
        "range15s": _range15s,
        "entry_price": _entry_p,
        "setup_variant": _variant,
        ...
    })
    continue
```

---

## Contexto: Outros Gates Já Implementados

1. **Cooldown de re-entrada 25s** (commit `7e13a66`) — bloqueia re-entrada imediata após exit
2. **passive_capture_too_late** (commit `2167209`) — bloqueia passive capture com <30s restantes
3. **leader_velocity_too_high** — bloqueia se mercado moveu muito rápido (existia antes)

O gate de qualidade de entrada seria o 4º gate, e potencialmente o mais impactante.

---

## Referência Rápida dos Scripts de Análise

| Script | O que faz |
|---|---|
| `analyze_entry_quality_on_logs.py` | **Este** — tiers de qualidade de entrada |
| `analyze_reentry_cooldown_on_logs.py` | Simula cooldown 25s nos logs |
| `analyze_chart_context_on_logs.py` | Chart context BTC (não preditivo para este setup) |
