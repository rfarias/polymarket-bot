# README_SCALP_REVERSAL

Este documento descreve o setup `scalp reversal` hoje implementado em `live_scalp_reversal_v1.py`.

## 1. Fonte de verdade

Arquivos principais:

- [run_scalp_reversal_bot.py](/C:/Users/Letícia/Documents/polymarket-bot/run_scalp_reversal_bot.py)
- [market/live_scalp_reversal_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/live_scalp_reversal_v1.py)
- [diagnostics_scalp_reversal_signal_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/diagnostics_scalp_reversal_signal_v1.py)

## 2. Ideia do setup

Esse setup tenta pegar reversao curta depois de extensao, escolhendo o lado mais barato entre `UP` e `DOWN` no slot observado.

Diferenca para o `current scalp`:

- ele nao usa referencia externa de BTC como fonte principal de edge
- ele se apoia mais no shape do book, stretch de preco e continuation filter
- ele e mais local ao microcomportamento do mercado da Polymarket

## 3. Como escolhe a entrada

A funcao `_choose_entry_candidate(...)` escolhe o lado de menor ask entre `UP` e `DOWN` e depois aplica bloqueios:

- preco de entrada nao pode estar acima de `stretch_price_max`
- spread nao pode estar largo demais
- profundidade minima precisa existir
- continuation filter nao pode indicar que o movimento ainda esta continuando com forca
- movimento recente nao pode estar monotonicamente esticado demais
- a aceleracao nao pode continuar forte
- precisa haver extensao previa suficiente para justificar reversao
- desequilibrio de book extremo bloqueia a entrada

Em resumo:

- entra no lado mais barato
- mas so quando a extensao parece cansada, nao quando a tendencia ainda esta limpa

## 4. Slots permitidos

O setup aceita:

- `next_1`
- `current`

Isso vem de `POLY_SCALP_ALLOWED_SLOTS`.

## 5. Maquina de estado

O setup usa `ScalpStateV1` com estados:

- `idle`
- `pending_entry`
- `open_position`
- `pending_exit`
- `done`

Na pratica, o ciclo relevante e:

- `idle` -> procura candidato
- `pending_entry` -> espera fill ou cancela por timeout
- `open_position` -> monitora `tp`, `stop` ou `timeout`
- `pending_exit` -> espera saida terminal ou cancela por timeout

## 6. Persistencia

O setup salva estado em:

- `runtime/polymarket_scalp_state_v1.json`

No bootstrap:

- restaura estado local
- bloqueia inicio se o broker ja tiver ordens abertas
- se houver estado local stale sem ordens abertas correspondentes, limpa o estado local

## 7. Guard rails atuais

O runner exige:

- `POLY_SCALP_ENABLED=true`
- ambiente guardado em real
- broker healthcheck ok
- nenhuma ordem aberta no broker no startup

Esse setup e mais restritivo no bootstrap do que outros: ele simplesmente nao inicia se ja houver open orders na conta.

## 8. Caminho paper -> real

O caminho recomendado para esse setup e:

1. Usar `diagnostics_scalp_reversal_signal_v1.py` para validar a logica de sinal.
2. Ajustar `stretch_price_max`, `max_spread`, `min_depth_top3`, `target_ticks` e `stop_ticks`.
3. Operar com tamanho minimo.
4. Verificar se a taxa de timeout nao esta alta demais.

## 9. Papel futuro no modo simultaneo

Hoje esse setup ainda esta menos pronto para simultaneidade do que o `next1 scalp` porque:

- bloqueia no startup se houver qualquer open order
- usa state file proprio mas sem reconciliacao rica com outros setups
- nao foi desenhado ainda como componente compartilhando a conta com varios runners reais ao mesmo tempo

Para coexistir com outros setups reais, ele precisara:

- adotar startup guard por ownership de ordens
- conviver com open orders de outros setups sem abortar tudo
- expor client keys mais padronizadas
- entrar em um coordenador comum de risco/ordens
