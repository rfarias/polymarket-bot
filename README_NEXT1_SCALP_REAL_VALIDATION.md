# README_NEXT1_SCALP_REAL_VALIDATION

Este documento e a checklist operacional de validacao real do setup `next1 scalp`.

Objetivo:

- nao assumir que o setup esta "pronto" apenas porque o codigo existe
- registrar evidencias reais por cenario
- bloquear expansao de rollout antes de validar caminhos criticos

## 1. Criterio de pronto

O setup `next1 scalp` so deve ser considerado validado para rollout real mais amplo quando:

- os cenarios criticos abaixo tiverem evidencias reais
- nao houver residuo de ordens ou posicao apos encerramento normal
- o restore tiver sido observado em caso real ou teste controlado
- os logs baterem com o estado real do broker

Enquanto isso nao acontecer, o setup deve ser tratado como:

- rollout real controlado
- tamanho minimo
- sem simultaneidade com outros setups reais

## 2. Configuracao recomendada para validacao inicial

Perfil conservador sugerido:

- `POLY_NEXT1_SCALP_REAL_ENABLED=true`
- `POLY_NEXT1_SCALP_AGGRESSIVE_QTY=1` ou o menor tamanho operacional viavel
- `POLY_NEXT1_SCALP_PASSIVE_QTY=0`
- `POLY_NEXT1_SCALP_ENTRY_TIMEOUT_SECS=25`
- `POLY_NEXT1_SCALP_ENTRY_REPRICE_SECS=1`
- `POLY_NEXT1_SCALP_EXIT_REPOST_SECS=6`
- `POLY_NEXT1_SCALP_POLL_SECS=0.5`

Regra pratica:

- validar primeiro com apenas perna agressiva
- so considerar perna passiva depois de validar bem entrada, saida e restore

## 3. Evidencias que devem ser guardadas

Para cada tentativa relevante, guardar:

- horario e slug do evento
- modo do trade no inicio e no fim
- trecho relevante do JSONL
- estado do broker antes e depois
- resultado observado
- se houve diferenca entre o esperado e o real

Fontes principais:

- `logs/next1_scalp_real_*/next1_scalp_real.jsonl`
- `logs/next1_scalp_real_state.json`
- saida do runner
- ordens reais vistas no broker

## 4. Checklist de preflight

Antes de qualquer sessao real:

- [ ] `python run_live_next1_scalp_real_v1.py --preflight-only` passou
- [ ] `POLY_GUARDED_ENABLED=true`
- [ ] `POLY_GUARDED_SHADOW_ONLY=false`
- [ ] `POLY_GUARDED_REAL_POSTS_ENABLED=true`
- [ ] `POLY_NEXT1_SCALP_REAL_ENABLED=true`
- [ ] broker healthcheck ok
- [ ] startup guard ok
- [ ] nenhuma open order inesperada na conta
- [ ] sizing configurado no minimo planejado

## 5. Checklist por cenario

## 5.1 Sem fill

Objetivo:

- confirmar que entrada pode expirar sem deixar lixo operacional

Validar:

- [ ] ordem de entrada foi postada
- [ ] nenhuma share foi preenchida
- [ ] timeout/cancelamento aconteceu
- [ ] state voltou para `idle`
- [ ] nao sobraram ordens abertas
- [ ] JSONL registrou `entry_cancel` ou evento equivalente

## 5.2 Fill total de entrada

Objetivo:

- confirmar transicao limpa de entrada para posicao aberta

Validar:

- [ ] ordem de entrada foi postada
- [ ] fill total ou suficientemente completo ocorreu
- [ ] state saiu de `working_entry` para `open_position`
- [ ] `entry_price_avg` ficou coerente
- [ ] stop e target foram calculados corretamente
- [ ] state file ficou consistente com o broker

## 5.3 Fill parcial de entrada

Objetivo:

- confirmar que o runner promove parcial para posicao sem perder controle

Validar:

- [ ] houve `size_matched` parcial
- [ ] sobra nao preenchida foi cancelada ou abandonada como previsto
- [ ] state promoveu para `open_position`
- [ ] quantidade remanescente da posicao bate com broker / saldo
- [ ] JSONL registrou `partial_fill_promoted` ou fluxo equivalente

## 5.4 Saida por target

Objetivo:

- confirmar take-profit real

Validar:

- [ ] state aberto atingiu `target_price`
- [ ] ordem de saida foi postada
- [ ] ordem de saida foi executada
- [ ] state voltou para `idle`
- [ ] nao restou saldo residual inesperado do token

## 5.5 Saida por stop

Objetivo:

- confirmar stop real

Validar:

- [ ] state aberto cruzou `stop_price`
- [ ] ordem de saida foi postada
- [ ] ordem foi executada ou zerada de forma consistente
- [ ] state voltou para `idle`
- [ ] perda observada foi coerente com o book e com a logica

## 5.6 Saida por timeout

Objetivo:

- confirmar encerramento por excesso de hold

Validar:

- [ ] posicao permaneceu aberta alem de `max_hold_secs`
- [ ] runner postou saida por `timeout`
- [ ] state limpou corretamente ao final
- [ ] nao sobraram ordens vivas

## 5.7 Saida por deadline

Objetivo:

- confirmar fechamento perto do fim do mercado

Validar:

- [ ] `active_secs` ficou dentro da zona de deadline
- [ ] runner postou saida por `deadline`
- [ ] state limpou corretamente
- [ ] nao houve resquicio de posicao apos o fim

## 5.8 Repost de saida

Objetivo:

- confirmar que a saida pendente pode ser cancelada e repostada sem perder controle

Validar:

- [ ] ordem de saida inicial ficou pendente
- [ ] runner executou cancelamento/repost
- [ ] quantidade nao ficou duplicada
- [ ] state file acompanhou corretamente a troca de `exit_order_id`
- [ ] posicao terminou zerada

## 5.9 Restore apos reinicio com entrada pendente

Objetivo:

- confirmar recovery em reinicio durante `working_entry`

Validar:

- [ ] processo foi interrompido com entrada viva
- [ ] state file foi restaurado
- [ ] runner reconheceu corretamente o status da ordem
- [ ] nao houve ordem duplicada
- [ ] fluxo seguiu para cancelamento, fill ou idle de modo consistente

## 5.10 Restore apos reinicio com saida pendente

Objetivo:

- confirmar recovery em reinicio durante `pending_exit`

Validar:

- [ ] processo foi interrompido com saida viva
- [ ] state file foi restaurado
- [ ] runner reconheceu `exit_qty_filled` corretamente
- [ ] state nao voltou erroneamente para entrada
- [ ] termino da posicao foi coerente

## 5.11 Panic cleanup em excecao

Objetivo:

- confirmar que uma excecao nao deixa risco solto

Validar:

- [ ] houve excecao com trade nao-idle
- [ ] JSONL registrou `exception` e `panic`
- [ ] ordens de entrada/saida vivas foram canceladas quando aplicavel
- [ ] se havia posicao, houve tentativa de `panic_exit`
- [ ] estado final ficou auditavel

## 6. Checklist de consistencia broker x state

Depois de cada sessao relevante:

- [ ] `entry_qty_filled` bate com `size_matched` observado
- [ ] `exit_qty_filled` bate com `size_matched` observado
- [ ] `remaining_position_qty` faz sentido
- [ ] saldo do token condicional nao ficou acima do esperado
- [ ] state file nao ficou preso em modo nao-idle sem justificativa
- [ ] nao restaram open orders inesperadas

## 7. Checklist de qualidade dos logs

Cada sessao relevante deve permitir responder:

- [ ] qual setup gerou a ordem
- [ ] qual foi o `event_slug`
- [ ] qual foi o lado operado
- [ ] qual foi o preco de entrada
- [ ] qual foi o preco de saida
- [ ] qual foi o motivo de saida
- [ ] houve reprice?
- [ ] houve restore?
- [ ] houve panic cleanup?

Se nao for possivel responder isso pelo log, a observabilidade ainda nao esta boa o suficiente.

## 8. Condicoes para expandir o rollout

Nao expandir size, frequencia ou complexidade enquanto faltar qualquer um destes:

- [ ] pelo menos um ciclo real completo de entrada e saida limpo
- [ ] pelo menos um cenario de fill parcial validado
- [ ] pelo menos um cenario de restore validado
- [ ] pelo menos um cenario de stop ou timeout validado
- [ ] nenhuma sobra operacional inesperada apos as sessoes

## 9. Condicoes para liberar proximo setup real

Nao liberar outro setup real enquanto o `next1 scalp` nao tiver:

- [ ] validacao real satisfatoria dos cenarios criticos
- [ ] comportamento estavel por varias sessoes
- [ ] confianca no restore e cleanup
- [ ] confianca de que nao deixa risco residual escondido

## 10. Registro manual de sessoes

Use este bloco para preenchimento manual por sessao:

### Sessao

- Data:
- Runner:
- Slug:
- Side:
- Setup:
- Qty:
- Resultado:
- Motivo de saida:
- Houve fill parcial:
- Houve restore:
- Houve repost de saida:
- Houve cleanup excepcional:
- Open orders finais:
- Saldo residual do token:
- Observacoes:

## 11. Decisao operacional

Se a maior parte da checklist ainda estiver vazia, o status correto do setup e:

- implementado
- armavel
- ainda em validacao real

Nao:

- pronto para simultaneidade
- pronto para aumento de size
- pronto para liberar outros setups reais
