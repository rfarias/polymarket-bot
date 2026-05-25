# SETUP: reversal_scalp — Requisitos de Implementação

## FILOSOFIA

Setup de scalp no lado perdedor de mercados quase resolvidos.
Diferença fundamental do reversal_sniper: NÃO espera resolução.
Compra o token perdedor barato, vende na valorização parcial (2x–3x).
Não depende do oracle confirmar reversão — depende do order flow do mercado.

Entrada: limit buy postado antes do sinal se materializar
Saída: limit sell no target OU stop OU deadline de tempo (T-15s)
Frequência esperada: maior que reversal_sniper (qualquer movimento serve)

FASE 1: PAPER ONLY — logging sem ordens reais.

---

## REQUISITO 1 — UNIVERSO DE MERCADOS

Mesmos critérios do reversal_sniper:
  - active_bid vencedor >= 0.88 e <= 0.99
  - Tempo restante: entre 20s e 120s
  - Volume total do mercado >= $500

Diferença: entrar mais cedo que o reversal_sniper (T-60s a T-30s)
porque precisa de tempo para o scalp se desenvolver e sair.

---

## REQUISITO 2 — SINAIS DE ENTRADA

Reutilizar os 4 sinais do reversal_sniper com os mesmos pesos.
threshold_entrada = score >= 3 (mais baixo que reversal_sniper)
porque não precisamos de certeza de resolução, só de movimento.

Adicionar Sinal E — Momentum do loser bid (peso 2):
  Se o loser bid já começou a subir (comparar com 2 polls atrás):
    loser_momentum = loser_bid_agora - loser_bid_2polls_atras
    Se loser_momentum > 0.005 → peso 1 (começando)
    Se loser_momentum > 0.015 → peso 2 (confirmado)

  Este sinal captura o início do movimento que queremos cavalgar.

---

## REQUISITO 3 — LÓGICA DE ENTRADA (limit buy)

entry_price_target = loser_bid_atual + 0.005
  (pagar levemente acima do bid atual para garantir fill)

Postar GTC limit buy no entry_price_target.
Timeout: cancelar se não preencheu em 8 segundos.
Se cancelado: registrar "entry_missed" no log e não tentar novamente
no mesmo mercado nesta janela.

Nunca entrar se loser_bid_atual > 0.15
  (movimento já aconteceu, edge foi embora)

---

## REQUISITO 4 — LÓGICA DE SAÍDA

Assim que buy confirmado, definir três saídas em paralelo:

TARGET (limit sell):
  target_multiplier = 2.0 (configurável: 1.5, 2.0, 2.5, 3.0)
  sell_price = entry_price × target_multiplier
  Postar GTC limit sell imediatamente após fill do buy

STOP (limit sell):
  stop_price = entry_price × 0.50
  Se loser_bid cair abaixo de stop_price → postar sell a mercado

DEADLINE (forçado):
  Se posição ainda aberta em T-15s antes de close_time:
    Cancelar todas as ordens pendentes
    Vender a mercado imediatamente
    Registrar razão: "deadline_exit"
  Aceitar qualquer preço — não ficar preso na resolução

Prioridade: TARGET > STOP > DEADLINE
Apenas uma das três executa. Assim que uma disparar, cancelar as outras.

---

## REQUISITO 5 — POSITION SIZING (paper)

paper_bet_size = $15 por trade
  (menor que reversal_sniper porque frequência maior = mais exposição)

shares_simuladas = paper_bet_size / entry_price_simulated

Máximo 2 posições simultâneas em mercados diferentes.
Nunca duas posições no mesmo event_slug.

---

## REQUISITO 6 — LOG JSONL

Arquivo: logs/reversal_scalp_paper_YYYYMMDD.jsonl

Eventos:
  "signal_detected"  → sinais calculados, would_enter bool
  "entry_posted"     → limit buy postado (simulado)
  "entry_filled"     → buy preenchido, posição aberta
  "entry_missed"     → buy não preencheu no timeout, cancelado
  "exit_target"      → saída no target (lucro)
  "exit_stop"        → saída no stop (perda parcial)
  "exit_deadline"    → saída forçada por tempo (qualquer PnL)
  "exit_resolution"  → mercado resolveu antes da saída (ganho ou perda total)

Campos em todo evento:
{
  "ts": float,
  "type": str,
  "event_slug": str,
  "winner_side": str,
  "active_bid_winner": float,
  "loser_bid": float,
  "time_remaining_secs": float,
  "btc_spot": float,
  "btc_divergence_pct": float,
  "signals": { sinal_a, sinal_b, sinal_c, sinal_d, sinal_e },
  "total_score": int,

  // em entry_filled:
  "entry_price": float,
  "shares": float,
  "bet_size": float,
  "target_price": float,
  "stop_price": float,

  // em exits:
  "exit_price": float,
  "exit_reason": str,
  "pnl_simulated": float,
  "return_pct": float,
  "hold_time_secs": float
}

---

## REQUISITO 7 — EXTENSÃO DO SCRIPT DE ANÁLISE RETROATIVA

Estender analyze_reversal_candidates_on_logs.py com:

Para cada evento de perda/bloqueio do current_almost_resolved,
calcular não só se houve reversão completa, mas também:

  loser_bid_max_during_window: float
    → pico máximo do loser bid entre o sinal e a resolução
    → requer reconstrução do histórico de bids (se disponível nos logs)
    → se não disponível: marcar como None

  scalp_would_have_worked_at_2x: bool
    → loser_bid_max >= loser_price_at_signal × 2.0

  scalp_would_have_worked_at_15x: bool
    → loser_bid_max >= loser_price_at_signal × 1.5

  max_return_available: float
    → (loser_bid_max / loser_price_at_signal - 1) × 100

Sumário adicional no terminal:
  Scalps 2x disponíveis: X% dos eventos
  Scalps 1.5x disponíveis: Y% dos eventos
  Return médio disponível nos eventos com movimento: Z%

---

## REQUISITO 8 — MÉTRICAS DE VALIDAÇÃO PARA MODO REAL

Após 150+ trades simulados:

  win_rate_target >= 25%        (target atingido)
  win_rate_any_profit >= 35%    (qualquer saída positiva)
  avg_return_wins >= 80%        (média dos trades vencedores)
  EV_por_trade >= $0.30         (win_rate × avg_gain - loss_rate × avg_loss)
  pct_deadline_exits <= 20%     (se muitos saem no deadline, timing errado)
  pct_entry_missed <= 30%       (se muitos buys não preenchem, preço errado)

Se pct_deadline_exits > 20%:
  → entrar mais cedo (aumentar janela de entrada para T-90s)
  → ou reduzir target_multiplier para 1.5x

Se pct_entry_missed > 30%:
  → ajustar entry_price_target para bid_atual + 0.010 (mais agressivo)

---

## ORDEM DE IMPLEMENTAÇÃO

1. Estender analyze_reversal_candidates_on_logs.py
   com loser_bid_max e scalp_would_have_worked
   → validar se scalps 2x existem em >= 20% dos eventos
   → se não: ajustar target para 1.5x e re-validar

2. market/live_reversal_scalp_v1.py (paper only)
   → Sinais A + B + E primeiro (divergência BTC, bid decel, loser momentum)
   → deadline exit implementado desde o início (crítico)
   → rodar 5–7 dias paper

3. Adicionar Sinais C e D
   → recalibrar threshold com dados reais

4. analyze_reversal_scalp_logs.py
   → análise específica dos logs do scalp
   → otimizar target_multiplier e timeout de entrada

5. Modo real apenas após Requisito 8 validado

---

## RELAÇÃO COM OS OUTROS SETUPS

reversal_scalp usa os MESMOS mercados e sinais do reversal_sniper.
A diferença é só na saída.

Podem rodar em paralelo? Sim, com cuidado:
  - Nunca abrir posição no mesmo event_slug nos dois setups simultaneamente
  - reversal_sniper tem prioridade se score >= 6 (tese mais forte = segurar até resolução)
  - reversal_scalp entra com score >= 3 se reversal_sniper não ativou

Bankroll completamente separado dos dois outros setups.
Logs completamente separados.

---

## REGRAS INEGOCIÁVEIS

1. DEADLINE EXIT É OBRIGATÓRIO — nunca deixar posição aberta na resolução sem intenção
2. Nunca market buy na entrada — sempre limit (controle de preço)
3. Position sizing fixo — nunca variar por "convicção"
4. Logs separados por data — nunca sobrescrever
5. Rodar analyze_reversal_candidates_on_logs.py com a nova coluna ANTES
   de implementar o runner — se scalps 2x < 15% dos eventos, revisar target
6. Se pct_deadline_exits > 40% nos primeiros 50 trades: pausar e revisar timing