# README_NEXT1_SCALP

Este documento descreve o setup `next1 scalp` como ele funciona hoje, quais arquivos são a fonte de verdade e quais cuidados operacionais devem ser preservados.

## 1. Fonte de verdade

Arquivos canônicos do setup:

- [run_live_next1_scalp_real_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/run_live_next1_scalp_real_v1.py): launcher com `--preflight-only`
- [market/live_next1_scalp_real_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/live_next1_scalp_real_v1.py): loop real completo
- [market/next1_scalp_signal_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/next1_scalp_signal_v1.py): sinal e parâmetros da pesquisa
- [diagnostics_next1_scalp_paper_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/diagnostics_next1_scalp_paper_v1.py): validação paper simples
- [diagnostics_next1_scalp_projection_paper_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/diagnostics_next1_scalp_projection_paper_v1.py): projeção paper por tamanhos

## 2. Ideia do setup

O `next1 scalp` é um setup direcional de curta duração sobre o slot `next_1`.

Ele usa:

- contexto do `current`
- contexto do `next_1`
- referência externa de BTC
- janela temporal específica do `next_1`
- controle de spread, profundidade e momentum

Objetivo operacional:

- entrar cedo o bastante para capturar continuação curta
- limitar chase de preço
- sair rápido por alvo, stop, timeout ou proximidade do fechamento do mercado

## 3. Máquina de estado

O runner real usa `LiveTradeState` com quatro estados:

- `idle`
  - nenhuma posição nem ordens ativas do setup

- `working_entry`
  - ordens de entrada postadas
  - pode haver perna agressiva e perna passiva
  - o loop acompanha fills, repricing e cancelamento por timeout

- `open_position`
  - houve fill total ou parcial suficiente para abrir posição
  - o runner recalcula alvo e stop e monitora saída

- `pending_exit`
  - ordem de saída já foi postada
  - o loop monitora fill e pode repostar a saída

Quando a posição zera, o estado volta para `idle`.

## 4. Entradas

O sinal vem de `Next1ScalpResearchV1.evaluate(...)`.

As travas principais do sinal são:

- `next1_secs` dentro da janela configurada
- divergência aceitável da referência externa
- spread máximo aceitável
- profundidade mínima no top 3
- warm-up mínimo para cálculo de deltas

O sinal retorna, entre outros campos:

- `allow`
- `side`
- `setup`
- `entry_price`
- `aggressive_entry_price`
- `exit_price`

O runner real usa esse sinal para postar entrada somente quando:

- o bot está em `idle`
- o setup está `allow=true`
- o `next_1` ainda está dentro da janela mínima
- o ambiente está armado para real

## 5. Estrutura de entrada

O setup suporta duas pernas de entrada:

- agressiva
  - compra no preço agressivo do sinal
  - controlada por `POLY_NEXT1_SCALP_AGGRESSIVE_QTY`

- passiva
  - compra passiva próxima do preço agressivo
  - controlada por `POLY_NEXT1_SCALP_PASSIVE_QTY`

Hoje o `.env` em uso está configurado com:

- `POLY_NEXT1_SCALP_AGGRESSIVE_QTY=6`
- `POLY_NEXT1_SCALP_PASSIVE_QTY=0`

Ou seja, a operação atual está preparada para usar apenas a perna agressiva.

## 6. Repricing e cancelamento de entrada

Enquanto o setup estiver em `working_entry`, o runner pode:

- detectar fill parcial ou total
- recalcular preço desejado
- cancelar e repostar a ordem remanescente
- cancelar entradas não preenchidas por:
  - timeout
  - invalidação do sinal
  - perda da janela temporal

Se houver fill parcial, o runner promove a posição para `open_position` e abandona a parte não executada.

## 7. Gestão de saída

Em `open_position`, a saída pode ser acionada por:

- `target`
- `stop`
- `deadline`
- `timeout`

Em `pending_exit`, o runner:

- acompanha `size_matched`
- fecha o estado quando a posição zera
- pode cancelar e repostar a ordem de saída se necessário

Também existe `_force_risk_cleanup(...)` para o caminho excepcional.

## 8. Persistência e recovery

O setup persiste o estado em:

- `logs/next1_scalp_real_state.json`

E grava a sessão em:

- `logs/next1_scalp_real_*/next1_scalp_real.jsonl`

No bootstrap:

- o runner tenta restaurar o estado anterior
- sincroniza fills com o broker
- reconcilia parte da posição com saldo de token
- se ainda existir estado não-idle restaurado, o runner se recusa a iniciar normalmente

Essa recusa é intencional. Ela força análise manual antes de continuar com posição ou ordens pendentes.

## 9. Guard rails obrigatórios

O preflight exige:

- `POLY_GUARDED_ENABLED=true`
- `POLY_GUARDED_SHADOW_ONLY=false`
- `POLY_GUARDED_REAL_POSTS_ENABLED=true`
- `POLY_GUARDED_ALLOW_NEXT_2=false`
- credenciais válidas do broker
- `broker healthcheck` ok
- `startup guard` ok

O startup guard também falha se houver ordens abertas externas ou com `client_key` desconhecida em status bloqueante.

## 10. Variáveis de ambiente relevantes

Variáveis específicas do setup:

- `POLY_NEXT1_SCALP_REAL_ENABLED`
- `POLY_NEXT1_SCALP_AGGRESSIVE_QTY`
- `POLY_NEXT1_SCALP_PASSIVE_QTY`
- `POLY_NEXT1_SCALP_ENTRY_TIMEOUT_SECS`
- `POLY_NEXT1_SCALP_ENTRY_REPRICE_SECS`
- `POLY_NEXT1_SCALP_EXIT_REPOST_SECS`
- `POLY_NEXT1_SCALP_POLL_SECS`
- `POLY_NEXT1_SCALP_RUN_SECONDS`

Variáveis globais que afetam o rollout:

- `POLY_GUARDED_ENABLED`
- `POLY_GUARDED_SHADOW_ONLY`
- `POLY_GUARDED_REAL_POSTS_ENABLED`
- `POLY_GUARDED_ALLOW_NEXT_2`
- `POLY_GUARDED_MIN_SHARES`

Credenciais do broker:

- `POLY_PRIVATE_KEY`
- `POLY_FUNDER`
- `POLY_API_KEY`
- `POLY_API_SECRET`
- `POLY_PASSPHRASE`
- `POLY_SIGNATURE_TYPE`

## 11. O que preservar ao evoluir o setup

- Preserve `preflight-only` como primeiro passo operacional.
- Preserve restore de estado antes de iniciar o loop real.
- Preserve a recusa de início quando houver estado restaurado não resolvido.
- Preserve `startup guard` antes de qualquer posting real.
- Preserve JSONL de sessão e state file para pós-análise.
- Preserve separação entre sinal e execução. O sinal não deve ganhar responsabilidade de broker.

## 12. O que não é fonte de verdade

Os arquivos abaixo ajudam análise e paper trading, mas não devem ser tratados como a implementação operacional canônica:

- `diagnostics_next1_scalp_paper_v1.py`
- `diagnostics_next1_scalp_projection_paper_v1.py`
- `run_next1_scalp_projection_paper_v1.py`

## 13. Forma recomendada de continuidade

Se outro programador for mexer neste setup, a ordem recomendada é:

1. Ler este documento.
2. Ler [market/next1_scalp_signal_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/next1_scalp_signal_v1.py).
3. Ler [market/live_next1_scalp_real_v1.py](/C:/Users/Letícia/Documents/polymarket-bot/market/live_next1_scalp_real_v1.py).
4. Rodar apenas `--preflight-only`.
5. Só então alterar sizing, janelas, sinal ou lógica de execução.
