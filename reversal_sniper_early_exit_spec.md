# SPEC: Saída Antecipada Inteligente — reversal_sniper

<!-- Cole o texto completo da spec aqui -->

Adicionar lógica de saída antecipada inteligente ao reversal_sniper.

CONTEXTO:
O reversal_sniper atualmente segura até resolução ou perde tudo.
Adicionar um terceiro estado: saída parcial quando a tese enfraquece.
Isso reduz perdas nos 87% que não resolvem completamente.

O QUE FAZER:

1. Adicionar campo "max_loser_bid_seen" ao estado da posição aberta
   → atualizar a cada poll com o pico máximo do loser bid desde entrada

2. Implementar função should_exit_early() com 3 condições:
   a) bid subiu >= 80% desde entrada E sinal_btc_divergence não está mais ativo
      → razão: "partial_profit_signal_faded"
   b) score total caiu abaixo de 2 E bid está acima do entry_price
      → razão: "score_collapsed_take_profit"  
   c) max_loser_bid_seen >= entry_price * 1.30 E bid atual < max_bid * 0.60
      → razão: "dynamic_stop_pullback"

3. Chamar should_exit_early() em todo poll da posição aberta
   → se retornar True: simular saída a mercado (loser_bid_atual)
   → registrar no JSONL com exit_reason

4. Adicionar ao JSONL dos trades que NÃO saíram antecipadamente:
   "max_loser_bid_seen": float
   "loser_bid_at_t20s": float
   "signal_btc_divergence_faded_at": timestamp ou null
   "min_score_during_hold": int

5. FASE PAPER ONLY — logging sem ordens reais
   Comparar EV total: sniper puro vs sniper com saída inteligente
   nos mesmos eventos

NÃO alterar a lógica de entrada, sinais, ou thresholds do sniper.
Apenas adicionar a camada de saída.
