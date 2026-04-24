# README_CURRENT_SETUPS

Este documento cobre os setups do slot `current`:

- `current scalp`
- `current almost resolved`
- como eles entram no `multi-setup`

## 1. Fonte de verdade

Arquivos principais:

- [run_live_current_scalp_real_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/run_live_current_scalp_real_v1.py)
- [market/live_current_scalp_real_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/live_current_scalp_real_v1.py)
- [market/current_scalp_signal_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/current_scalp_signal_v1.py)
- [market/current_almost_resolved_signal_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/current_almost_resolved_signal_v1.py)
- [market/live_multi_setup_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/live_multi_setup_v1.py)
- [run_multi_setup_bot.py](/C:/Users/Letícia/Documents/polymarket-bot/run_multi_setup_bot.py)

## 2. Current scalp

### 2.1 Ideia

O `current scalp` tenta pegar um movimento curto dentro do mercado atual usando:

- referencia externa de BTC
- distancia do spot em relacao ao preco de abertura do candle/evento
- momentum recente de 5s e 15s
- spread e profundidade minima

### 2.2 Duas familias de sinal

O modulo `CurrentScalpResearchV1` procura dois tipos de setup:

- `continuation`
  - o mercado ja esta do lado do movimento desde a abertura
  - o spot segue acelerando na mesma direcao
  - o contrato ainda nao esta caro demais

- `reversal`
  - houve extensao demais contra um lado
  - o spot comeca a voltar no curtissimo prazo
  - o preco do lado candidato ainda esta barato o suficiente

### 2.3 Filtros principais

Antes de liberar qualquer entrada, o setup exige:

- tempo minimo desde a abertura
- tempo minimo ate o fim
- spread controlado
- profundidade suficiente
- divergencia aceitavel entre fontes externas
- preco de abertura externo disponivel
- warm-up de historico para calculo de deltas

### 2.4 Execucao atual

Hoje o `current scalp` ja tem runner real dedicado proprio em `live_current_scalp_real_v1.py`.

Ele tambem entra pelo `live_multi_setup_v1.py`, onde:

- pode ficar em `shadow_only`
- pode abrir uma unica posicao simples no `current`
- usa `target`, `stop`, `expiry_near` e `timeout`

Estado pratico da posicao:

- `idle`
- `pending_entry`
- `open_position`
- `pending_exit`

## 3. Current almost resolved

### 3.1 Ideia

Esse setup tenta capturar ticks finais quando o `current` ja esta quase resolvido para um lado, mas ainda sem sinais fortes de reversao.

### 3.2 O que ele procura

O modulo `evaluate_current_almost_resolved_v1(...)` procura:

- um lado lider muito caro, mas ainda dentro de faixa de entrada
- lado oposto bem barato
- spread curto
- profundidade suficiente
- contexto do spot ainda compativel com continuidade do lider

Na pratica:

- compra o lado que ja esta quase resolvendo
- tenta capturar mais um tick curto
- usa hold curto e stop mais largo que o alvo

### 3.3 Janela operacional

Esse setup so opera numa faixa curta de tempo restante:

- depois do miolo do mercado
- antes de ficar tarde demais para um scalp limpo

## 4. Como o multi-setup decide

`live_multi_setup_v1.py` junta tres coisas:

- fill-cycle hedgeado de `next_1`
- `current scalp`
- `current almost resolved`

A regra atual de prioridade e:

- o `next_1` continua sendo a estrategia principal
- se houver plano ativo em `next_1`, o bot pode usar isso como gatilho para habilitar `current scalp`
- `almost_resolved` entra como fallback tatico quando a janela do `next_1` aborta cleanly por deadline

Mais especificamente:

- se existe plano ativo em `next_1` e o `current scalp` esta permitido, o bot pode abrir uma posicao simples no `current`
- se houve janela pendente de `almost_resolved` apos cleanup do `next_1` e o sinal estiver valido, ele pode abrir essa alternativa

## 5. Flags operacionais atuais

No `run_multi_setup_bot.py`, os setups do `current` ja possuem flags separadas:

- `POLY_CURRENT_SCALP_SHADOW_ONLY`
- `POLY_CURRENT_ALMOST_RESOLVED_SHADOW_ONLY`

Isso e importante porque permite:

- manter `next_1` real
- manter `current` em paper/shadow
- promover cada setup do `current` individualmente para real depois

## 6. Caminho recomendado paper -> real

Para cada setup do `current`:

1. Manter o modulo de sinal como fonte de verdade.
2. Criar ou fortalecer diagnosticos paper especificos.
3. Rodar dentro do `multi-setup` em `shadow_only=true`.
4. No caso do `current scalp`, usar o runner dedicado para preflight e rollout real controlado.
5. Revisar taxa de entrada, tempo de hold e motivos de stop/timeout.
6. So depois ligar o real para aquele setup de forma recorrente.

## 7. Papel futuro no modo simultaneo

Os setups do `current` ainda nao estao no mesmo nivel de maturidade operacional do `next1 scalp` ou do `fill-cycle`.

Para virar setups reais simultaneos com seguranca, ainda faltam:

- runner real dedicado para `current almost resolved` ou uma maquina de estado mais explicita no `multi-setup`
- persistencia dedicada de estado para as posicoes do `current`
- restore/recovery equivalente ao padrao do `next1 scalp`

Hoje o `current scalp` ja deu o primeiro passo com runner real dedicado. O que falta e endurecer o restante da camada operacional, sobretudo para o `almost resolved`.

## 8. Overlay manual

Existe agora um overlay desktop simples para suporte a operacao manual:

- runner: `run_manual_overlay_v1.py`
- fonte de contexto: `market/current_scalp_signal_v1.py`
- fonte de decisao almost resolved: `market/current_almost_resolved_signal_v1.py`

Uso:

```bash
python run_manual_overlay_v1.py
```

O overlay atualiza a cada ~2s e mostra:

- direcao/tendencia inferida do spot
- risco de reversao
- distancia ate o `price to beat`
- buffer restante depois do pullback adverso
- leitura operacional `SAFE`, `CAUTION`, `UNSAFE` ou `BLOCKED`

### 8.1 Modo browser assist

Para operar manualmente sem cobrir a interface, existe tambem um bridge local + userscript:

- servidor local do sinal: `run_manual_signal_server_v1.py`
- userscript do navegador: `scripts/polymarket_manual_assist.user.js`

Uso:

```bash
python run_manual_signal_server_v1.py --qty 6
```

Depois carregue o userscript no navegador.

Esse modo:

- desenha um painel compacto acima do grafico
- limita a leitura a uma janela util por mercado de 5m
- pode preencher `lado`, `preco` e `quantidade`
- nao clica em `Trade`
- deixa o clique final e a confirmacao da wallet com voce
