# README_MANUAL_ADOPT_CURRENT_ALMOST_RESOLVED

Este documento descreve o modo `manual adopt` para o setup `current_almost_resolved`.

Objetivo:

- deixar a entrada sob controle manual
- deixar o bot assumir somente a gestao da saida
- reduzir erro operacional em `stop`, `profit protect` e `flatten`

## 1. Modelo operacional

Fluxo desejado:

1. o setup `current_almost_resolved` fica armado
2. o operador posta uma ordem limite manual
3. o bot detecta essa ordem na conta
4. o bot vincula a ordem ao setup ativo
5. quando houver fill parcial ou total, o bot passa a gerenciar a saida

O bot nao deve entrar automaticamente.

## 2. Regra mais importante

Para este modo ser confiavel, o bot nao deve estimar o preco de entrada pelo sinal atual nem pelo saldo de token.

O preco de entrada precisa vir de fonte exata:

- ordem limite manual identificada pelo `order_id`
- preco da propria ordem
- `size_matched` real da ordem
- historico autenticado de trades/fills da conta quando necessario

1 tick de erro ja muda o resultado de lucro para prejuizo em setups curtos.

Por isso, o desenho correto e:

- adocao por ordem manual aberta
- acompanhamento por `order_id`
- quantidade real por `size_matched`
- preco real pelo fill/ordem correspondente

Se o bot nao conseguir identificar a ordem manual exata, ele nao deve adotar a posicao.

## 3. Por que limite manual facilita

Como a entrada e feita so por ordem limite:

- o preco alvo da entrada ja existe antes do fill
- o bot pode identificar a ordem antes mesmo do preenchimento
- o fill parcial pode ser acompanhado de forma exata
- a gestao passa a respeitar a quantidade realmente preenchida

Isso e mais confiavel do que detectar so pelo saldo de cotas apos o fill.

## 4. Guard rails recomendados

Para a adocao automatica funcionar com seguranca:

- operar um setup por vez
- adotar apenas o mercado `current` esperado
- adotar apenas uma ordem manual por vez
- exigir `BUY` no lado coerente com o sinal atual
- exigir ausencia de ordens manuais de saida ja abertas
- ignorar saldo residual antigo
- exigir janela curta de adocao
- recusar adocao se o fill exato nao puder ser identificado

## 5. Gestao depois da adocao

Depois da adocao, o bot fica responsavel por:

- `stop`
- `profit_protect`
- `target`, quando fizer sentido
- `deadline_flatten`
- repost de saida, se necessario

No caso do `resolved_pullback_limit`:

- se estiver perto do fim e a estrutura continuar limpa, o ideal e deixar o mercado resolver
- a realizacao antecipada so deve acontecer quando houver risco claro de reversao maior

## 6. Estado atual no repositorio

Arquivos relacionados:

- [market/manual_adopt_current_almost_resolved_v1.py](C:/Users/Romario/Desktop/BACKUP%20ROMÁRIO/documentos/polymarket-bot/market/manual_adopt_current_almost_resolved_v1.py)
- [run_manual_adopt_current_almost_resolved_v1.py](C:/Users/Romario/Desktop/BACKUP%20ROMÁRIO/documentos/polymarket-bot/run_manual_adopt_current_almost_resolved_v1.py)
- [market/live_current_almost_resolved_real_v1.py](C:/Users/Romario/Desktop/BACKUP%20ROMÁRIO/documentos/polymarket-bot/market/live_current_almost_resolved_real_v1.py)

Observacao importante:

- a versao atual ja estrutura o modo `manual adopt`
- mas o criterio ideal de adocao e por ordem limite manual/fill exato
- isso deve ter prioridade sobre qualquer estimativa derivada do saldo

## 7. Proximo passo recomendado

Evoluir a adocao para:

- detectar ordem limite manual aberta
- vincular por `order_id`
- usar `size_matched` como quantidade real adotada
- buscar o fill exato no historico autenticado de trades da conta
- so assumir a posicao quando esses dados forem consistentes

Esse e o padrao correto para uso real em setups de poucos ticks.
