# Handoff 2026-05-18 Tarde — Análise de Perdas + Cooldown de Re-entrada

## Contexto

Continuação da sessão da manhã. Na tarde, o foco foi entender POR QUE as perdas do setup
`current_almost_resolved` ocorrem na prática real, mesmo com 96%+ de win rate.

---

## Problema Central Identificado

**Paper vs real não é apenas otimismo estatístico — é assimetria de payoff estrutural.**

- Wins reais: +5 a +15 USD por trade
- Losses reais: -200 a -500 USD por trade (alguns -700 USD)
- 96%+ WR ainda resulta em -700 USD acumulado (conta rfarias, operações manuais)

**Causa raiz:** Em Polymarket, quando o mercado reverte próximo da resolução, o book some.
Não há bids intermediários. Um stop em 0.93 pode preencher a 0.57 — ou não preencher e a posição
vai a zero. O paper model assume `fill at stop_price`, o que é falso.

---

## Análise dos Logs Paper — Chart Context

Rodamos `analyze_chart_context_on_logs.py` nos logs disponíveis aqui (PC de desenvolvimento).

**Dataset:** `logs/all_setups_paper_until_20260427_0700_v2/` — 1146 trades totais

**Resultado crítico:** Os logs são uma mistura de setups:
- `current_almost_resolved`: 289 trades, **96.5% WR**, 279W/10L, +470 ticks total
- `next1_scalp`: 857 trades, 64.6% WR — **fonte de todos os -49/-47 tornados**

Os 3 maiores tornados (-49, -47, -47) são **todos next1_scalp**, com `last_bid=0.0` (book gap completo).
Ignorar por enquanto — `next1_scalp` tem problema estrutural diferente.

**Conclusão sobre chart context:** NÃO é preditivo para perdas de `current_almost_resolved`.
- threshold 0.50: WR cai 72.4% → 70.0% (piora porque filtra trades bons)
- threshold 0.80: neutro
- As 10 perdas do setup não coincidem com padrões de candle conflitantes

---

## Análise das 10 Perdas do current_almost_resolved

Examinamos o contexto de 120s antes de cada perda. Quatro padrões distintos:

### Padrão 1 — Re-entrada Imediata (4/10 perdas) ← IMPLEMENTADO HOJE

Trade 1 sai (timeout ou stop) → sinal re-dispara imediatamente → re-entrada no pico de preço.

Exemplos:
- Trade 1 sai +7 ticks (timeout) → re-entra IMEDIATAMENTE em 0.96 → cai para 0.90 → -5 ticks
- Trade 1 sai +3 ticks (timeout) → re-entra IMEDIATAMENTE em 0.97 → cai para 0.93 → -3 ticks
- Dois trades no mesmo round: sai um, re-entra o próximo poll

**Solução implementada:** Cooldown de 25s após cada saída de posição real.

### Padrão 2 — Entrada em Desaceleração (3/10 perdas) ← PRÓXIMO

O bid estava MAIS ALTO 9-15s antes da entrada. Sinal dispara quando momentum já inverteu.

**Solução planejada (não implementada):** Gate de desaceleração — bloquear se `bid_now < bid_N_polls_ago`.
Implementar no runner, comparando `active_bid` atual com `active_bid` de 3-4 polls atrás.

### Padrão 3 — Book Volátil na Entrada (1/10 perdas — a de -13 ticks)

Mercado oscilando ±0.06-0.08 antes da entrada. `market_range_15s = 0.06` no momento da entrada.
Exit 10s depois com last_bid=0.80 → -13 ticks.

**Solução planejada (não implementada):** Gate de volatilidade — bloquear se `range_15s >= 0.04`.
Já existe `vel_range_30` no runner (threshold atual 0.04) — pode ser que este gate já cubra.

### Padrão 4 — Stop de 1 Tick Prematuro (2/10 perdas)

Mercado dipa 1 tick, stop dispara, mercado recupera em 10-17s. Saída desnecessária.

**Solução planejada (não implementada):** Confirmação de 2 polls antes de executar stop.
Com runner a 0.5s de polling: aguarda 1s de confirmação antes de postar saída.
Risco: em reversão real, 1s extra de delay pode piorar o fill.

---

## Implementação: Cooldown de Re-entrada (Padrão 1)

**Arquivo:** `market/live_current_almost_resolved_real_v1.py`

**O que foi feito:**

```python
# Inicialização (linha ~1052):
_reentry_blocked_until: dict[str, float] = {}
reentry_cooldown_secs: float = 25.0
```

**Check no bloco de entrada idle** (após `blocked_entry_events` existente):
```python
if event_slug and _reentry_blocked_until.get(event_slug, 0.0) > now:
    _remaining = round(_reentry_blocked_until[event_slug] - now, 1)
    _append_jsonl(log_path, {
        "type": "entry_blocked",
        "reason": "reentry_cooldown",
        "cooldown_remaining_secs": _remaining,
        ...
    })
    time.sleep(poll_secs)
    continue
```

**Setters de cooldown** (antes de cada reset `trade = LiveCurrentAlmostResolvedTradeState()`
onde `entry_qty_filled > 0`):
```python
if trade.event_slug and _safe_float(trade.entry_qty_filled) > 0:
    _reentry_blocked_until[str(trade.event_slug)] = now + reentry_cooldown_secs
```

**6 pontos de reset cobertos:**
1. `pending_exit` flat sem exit_order
2. `pending_exit` flat com exit_order preenchida
3. `exit_pending_confirm` flat
4. `awaiting_redeem` redeem_flat
5. `awaiting_redeem` redeem_dust_archived
6. `external_close_detected`

**Log de diagnóstico:** Cada entrada bloqueada pelo cooldown gera evento `entry_blocked`
com `reason: "reentry_cooldown"` e `cooldown_remaining_secs` no JSONL.

---

## O Que Fazer no PC de Casa

### Passo 1: Atualizar

```powershell
git pull
git log --oneline -5
python -m py_compile market/live_current_almost_resolved_real_v1.py
```

### Passo 2: Rodar normalmente

O runner real NÃO muda de comportamento aparente — apenas bloqueia re-entradas imediatas.

```powershell
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 6 -RunSeconds 1800 -PollSeconds 0.5
```

### Passo 3: Monitorar nos logs

Procurar eventos `entry_blocked` com `reason: "reentry_cooldown"`:

```powershell
Select-String -Path logs\current_almost_resolved_real_*\*.jsonl -Pattern "reentry_cooldown"
```

### Passo 4: Avaliar próximas proteções

Após acumular sessões com o cooldown ativo:
1. **Gate de desaceleração** — bid caindo → não entrar
2. **Gate de volatilidade** — range_15s >= 0.04 → não entrar (checar se já coberto pelo vel_range_30)
3. **Confirmação de stop** — 2 polls antes de postar saída a stop

**Não implementar sem ver o efeito do cooldown primeiro.**

---

## Arquivos Modificados Nesta Tarde

```text
MOD  market/live_current_almost_resolved_real_v1.py  (re-entry cooldown 25s)
NOVO HANDOFF_2026_05_18_TARDE.md (este arquivo)
```

---

## Resumo para o Agente

```text
Setup atual: current_almost_resolved (96.5% WR paper, mas -700 USD real por assimetria de payoff)

Causa das perdas reais:
1. Re-entrada imediata após saída → implementado: cooldown 25s em _reentry_blocked_until
2. Entrada em desaceleração → planejado: gate bid_now < bid_N_polls_ago
3. Book volátil → planejado: gate range_15s >= 0.04
4. Stop prematuro 1 tick → planejado: confirmação 2 polls

Chart context BTC: NÃO preditivo para este setup. Manter como logging apenas.
Fase 2 chart context (blocking): NÃO implementar — não há correlação nas perdas reais.

Próxima sessão: verificar se reentry_cooldown aparece nos logs reais e se reduziu losses.
```
