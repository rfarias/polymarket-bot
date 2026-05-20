# HEDGE DE PROTEÇÃO — Requisitos de Implementação
# Setup: current_almost_resolved

## CONTEXTO E FILOSOFIA

O current_almost_resolved sofre de um problema estrutural conhecido:
quando o mercado reverte após a entrada, o book do lado vencedor some
(last_bid = 0.0) e o stop falha completamente. A posição vai a zero.

O hedge é uma camada de proteção adicional para exatamente esse cenário:
comprar o lado perdedor quando o stop falha, travando a perda máxima
independente do resultado da resolução.

O hedge NÃO resolve a assimetria fundamental do setup
(ganho pequeno vs. risco grande). Ele mitiga o pior cenário:
trocar perdas totais por book gap (-$93 numa entrada de $93)
por perdas controladas e previsíveis (-$10 a -$40).

FASE 1: ANÁLISE RETROATIVA NOS LOGS EXISTENTES
Antes de qualquer implementação no runner, rodar o script
analyze_hedge_impact_on_logs.py para verificar se o hedge
teria ajudado nos eventos históricos de perda.
Se o impacto for negativo ou neutro: não implementar no runner.

---

## FASE 1 — SCRIPT DE ANÁLISE RETROATIVA

Arquivo: analyze_hedge_impact_on_logs.py

Inputs:
  --logs-dir <diretório com logs JSONL do current_almost_resolved>
  --output <arquivo CSV de saída>

Para cada evento de PERDA nos logs (stop atingido, book gap, loss total):

  1. Identificar o momento do sinal de invalidade:
     - Quando bid_decel_gate.would_block tornou-se true APÓS a entrada
     - Quando last_bid caiu para 0.0 (book gap detectado)
     - Quando total_score caiu abaixo do threshold de entrada

  2. Para esse momento, extrair do log:
     - active_bid_winner: float  (preço do lado vencedor)
     - loser_bid_estimated: 1 - active_bid_winner
     - time_remaining_secs: float
     - razão do sinal de invalidade

  3. Calcular custo e resultado do hedge hipotético:
     shares_originais = bet_size / entry_price
     hedge_price = loser_bid_estimated  (preço estimado de compra)
     custo_hedge = shares_originais × hedge_price
     custo_total = bet_size + custo_hedge
     max_loss_locked = custo_total - (shares_originais × 1.00)

     Se max_loss_locked < 0: hedge seria lucrativo
     Se max_loss_locked > 0: hedge trava perda em max_loss_locked

  4. Comparar com a perda real registrada no log:
     perda_real = valor registrado no evento de loss
     impacto_hedge = perda_real - max_loss_locked
     Se impacto_hedge > 0: hedge teria reduzido a perda
     Se impacto_hedge < 0: hedge teria piorado

  5. Verificar se hedge seria viável (loser_bid <= 0.40):
     hedge_viable = loser_bid_estimated <= 0.40

Output CSV colunas:
  timestamp, event_slug, entry_price, bet_size,
  loss_real, loser_bid_at_signal, hedge_price,
  custo_hedge, max_loss_locked, impacto_hedge,
  hedge_viable, signal_trigger, time_remaining_at_signal

Sumário obrigatório no terminal ao final:

  ══════════════════════════════════════════════
  ANÁLISE DE IMPACTO DO HEDGE
  ══════════════════════════════════════════════
  Total de eventos de perda analisados: N

  Hedge viável (loser_bid <= 0.40): X (Y%)
  Hedge inviável (loser_bid > 0.40): Z (W%)

  Nos eventos onde hedge era viável:
    Perda média SEM hedge:       -$XX.XX
    Perda média COM hedge:       -$XX.XX
    Redução média de perda:      $XX.XX (XX%)
    Melhor caso (menor perda):   -$X.XX
    Pior caso (hedge piorou):    -$XX.XX

  Breakdown por trigger do sinal de invalidade:
    book_gap (last_bid=0.0):     N eventos, impacto médio $X
    bid_decel gate:              N eventos, impacto médio $X
    score_collapsed:             N eventos, impacto médio $X

  VEREDICTO:
    Se redução média >= $10 em >= 60% dos eventos viáveis:
      ✅ HEDGE RECOMENDADO — implementar no runner
    Caso contrário:
      ❌ HEDGE NÃO RECOMENDADO — impacto insuficiente
  ══════════════════════════════════════════════

---

## FASE 2 — IMPLEMENTAÇÃO NO RUNNER
## (apenas se análise retroativa recomendar)

Arquivo: market/live_current_almost_resolved_real_v1.py

LÓGICA DE PROTEÇÃO EM CAMADAS (ordem de prioridade):

  Camada 1 — Stop normal (já existe)
  Camada 2 — Hedge (novo, fallback quando stop falha)

  A cada poll com posição aberta:

    PASSO 1: verificar se stop deve ser tentado
      Se active_bid_winner < stop_price:
        tentar sell a mercado (lógica existente)

    PASSO 2: se stop não preencheu em 2 polls consecutivos
      (last_bid == 0.0 ou fill não confirmado)
      → verificar elegibilidade do hedge:

        should_hedge = (
          loser_bid_atual <= 0.40
          AND time_remaining > 8s   (tempo para executar)
          AND hedge_not_yet_done    (nunca hedgear duas vezes)
        )

        Se should_hedge:
          executar hedge (ver EXECUÇÃO abaixo)

    PASSO 3: com posição hedgeada
      não tentar mais stops nem ajustes
      aguardar resolução com perda travada

GATILHOS PARA HEDGE (qualquer um é suficiente):

  Gatilho A — Book gap confirmado
    last_bid == 0.0 por 2 polls consecutivos
    E stop foi tentado sem sucesso

  Gatilho B — Sinal primário invertido
    signal_btc_divergence.active == False (sumiu após entrada)
    E loser_bid subiu >= 0.05 desde entrada
    E loser_bid_atual <= 0.40

  Gatilho C — Score colapsou
    total_score < (entry_score_threshold - 1)
    E bid_vencedor caindo (bid_velocity < -0.02)
    E loser_bid_atual <= 0.30

  Limite absoluto:
    NUNCA hedgear se loser_bid_atual > 0.40
    Acima desse preço o hedge piora mais do que ajuda.

EXECUÇÃO DO HEDGE:

  shares_hedge = shares_originais  (mesma quantidade)
  limit_price  = loser_bid_atual + 0.01  (pagar levemente acima)

  Postar GTC limit buy no lado perdedor.
  Timeout: 5 segundos para fill.
  Se não preencheu: tentar market buy (prioridade é travar a perda).

CÁLCULO DA PERDA TRAVADA (registrar no log):

  custo_total      = (shares × entry_price) + (shares × hedge_price)
  receita_maxima   = shares × $1.00
  max_loss_locked  = custo_total - receita_maxima

  Exemplo:
    100 shares UP a $0.93  = $93.00
    100 shares DOWN a $0.18 = $18.00
    custo_total = $111.00
    receita_maxima = $100.00
    max_loss_locked = -$11.00  (perda máxima travada em $11)

---

## LOGGING ADICIONAL NO JSONL

Adicionar aos eventos existentes do runner:

Em todo snapshot com posição aberta:
  "hedge_eligible": bool       (loser_bid <= 0.40 e tempo > 8s)
  "loser_bid_current": float   (preço atual do lado perdedor)
  "hedge_status": "none" | "pending" | "filled" | "failed"

Em evento de tentativa de hedge:
  {
    "type": "hedge_attempted",
    "trigger": str,              (gatilho que ativou)
    "loser_bid_at_trigger": float,
    "shares_hedge": float,
    "limit_price": float,
    "time_remaining": float
  }

Em evento de hedge preenchido:
  {
    "type": "hedge_filled",
    "hedge_price": float,
    "custo_hedge": float,
    "custo_total_posicao": float,
    "max_loss_locked": float,
    "ts_hedge": float
  }

Em resolução com hedge ativo:
  {
    "type": "resolution_hedged",
    "resolution": "UP" | "DOWN",
    "pnl_final": float,
    "max_loss_was_locked": float,
    "hedge_worked": bool   (pnl_final >= -max_loss_locked - 1.0)
  }

---

## MÉTRICAS DE VALIDAÇÃO NOS RELATÓRIOS DO AGENTE

O optimizer_loop.py deve incluir nos relatórios:

  Seção: Efetividade do Hedge (current_almost_resolved)

  Tentativas de hedge: N
  Hedges executados com sucesso: X (Y%)
  Hedges que falharam (loser sem liquidez): Z

  Comparativo de perdas:
    Stops bem-sucedidos:     N trades | perda média $X
    Book gap SEM hedge:      N trades | perda média $X
    Book gap COM hedge:      N trades | perda média $X

  Redução de perda pelo hedge: $X por evento (média)
  Impacto no PnL total: +$X / -$X vs. sem hedge

  Se hedge_worked < 60% dos casos:
    ⚠️  Hedge com baixa efetividade — revisar gatilhos

---

## ORDEM DE IMPLEMENTAÇÃO

1. analyze_hedge_impact_on_logs.py
   → rodar nos logs existentes ANTES de qualquer outra coisa
   → se veredicto for ❌: parar aqui, não implementar

2. Se veredicto for ✅:
   → adicionar campos de logging ao runner (sem lógica de hedge ainda)
   → coletar dados de loser_bid e hedge_eligible por 48h
   → confirmar que loser_bid <= 0.40 em >= 50% dos eventos de perda

3. Implementar lógica de hedge no runner (paper mode primeiro)
   → só Gatilho A (book gap) inicialmente
   → validar por 72h se max_loss_locked está sendo respeitado

4. Adicionar Gatilhos B e C após validação do Gatilho A

5. Ativar em modo real apenas após:
   hedge_worked >= 60% dos casos em paper
   perda_média_com_hedge < perda_média_sem_hedge

---

## REGRAS INEGOCIÁVEIS

1. ANÁLISE RETROATIVA OBRIGATÓRIA ANTES DE QUALQUER IMPLEMENTAÇÃO
   Se logs mostrarem impacto negativo ou neutro: não implementar.

2. NUNCA hedgear se loser_bid > 0.40
   Acima desse preço o custo do hedge supera o benefício.

3. NUNCA hedgear duas vezes na mesma posição
   flag hedge_not_yet_done deve ser verificada sempre.

4. NUNCA hedgear sem tempo suficiente
   time_remaining deve ser > 8s para execução segura.

5. O hedge é FALLBACK do stop, não substituto
   Sempre tentar stop primeiro. Hedge só se stop falhou.

6. Logs separados por data — nunca sobrescrever histórico.
