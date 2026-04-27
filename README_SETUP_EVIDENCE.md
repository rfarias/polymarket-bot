# README_SETUP_EVIDENCE

Este documento registra evidencias empiricas dos setups a partir de papers e logs versionados no repositorio.

Objetivo:

- manter uma base cumulativa de aprendizado
- comparar setups por contexto de mercado
- reaproveitar experiencias passadas na calibragem de estrategia

## 1. Fonte dos dados

Janela principal analisada:

- inicio: `2026-04-25`
- termino: `2026-04-27 07:00` em `America/Sao_Paulo`

Logs principais usados:

- [session_summary.json](C:\Users\Romario\Desktop\BACKUP%20ROMÁRIO\documentos\polymarket-bot\logs\all_setups_paper_until_20260427_0700_v2\session_summary.json)
- [all_setups_paper_until_20260427_0700_v2](C:\Users\Romario\Desktop\BACKUP%20ROMÁRIO\documentos\polymarket-bot\logs\all_setups_paper_until_20260427_0700_v2)
- [next1_scalp_paper_until_20260427_0700.jsonl](C:\Users\Romario\Desktop\BACKUP%20ROMÁRIO\documentos\polymarket-bot\logs\next1_scalp_paper_until_20260427_0700.jsonl)

Arquivos de logica relacionados:

- [diagnostics_all_setups_paper_v1.py](C:\Users\Romario\Desktop\BACKUP%20ROMÁRIO\documentos\polymarket-bot\diagnostics_all_setups_paper_v1.py)
- [diagnostics_next1_scalp_paper_v1.py](C:\Users\Romario\Desktop\BACKUP%20ROMÁRIO\documentos\polymarket-bot\diagnostics_next1_scalp_paper_v1.py)
- [market/current_almost_resolved_signal_v1.py](C:\Users\Romario\Desktop\BACKUP%20ROMÁRIO\documentos\polymarket-bot\market\current_almost_resolved_signal_v1.py)
- [market/current_scalp_signal_v1.py](C:\Users\Romario\Desktop\BACKUP%20ROMÁRIO\documentos\polymarket-bot\market\current_scalp_signal_v1.py)
- [market/next1_scalp_signal_v1.py](C:\Users\Romario\Desktop\BACKUP%20ROMÁRIO\documentos\polymarket-bot\market\next1_scalp_signal_v1.py)

## 2. Metodologia

Cada trade foi ligado ao snapshot imediatamente anterior a entrada.

Os regimes foram classificados por:

- tendencia: `alta`, `baixa`, `lateral`
- forca: `fraca`, `media`, `forte`
- volatilidade: `compressao`, `moderada`, `alta`
- padrao: `tendencia_limpa`, `pullback_controlado`, `impulso_volatil`, `reversao_ou_serrote`, `lateralizacao`
- fase do candle: `cedo`, `meio`, `intermediaria`, `tardia`

Regras praticas usadas na classificacao:

- tendencia baseada na distancia do spot em relacao a abertura
- forca baseada no tamanho absoluto dessa distancia
- volatilidade baseada em `market_range_30s`
- padrao baseado em alinhamento entre spot de curtissimo prazo e movimento do mercado
- fase baseada em `secs_to_end`

Observacao:

- o hedge de `next_1` nao foi resumido como PnL simples neste estudo
- para o hedge, o foco foi `plan_created`, `done` e `force_closed`

## 3. Resumo bruto

Resultado total do `all_setups`:

- rounds vistos: `546`
- trades fechados: `1146`

Por setup:

- `next1_scalp`: `822` trades | `554` wins | `62` losses | `206` flats | `+620 ticks`
- `current_almost_resolved`: `289` trades | `273` wins | `10` losses | `6` flats | `+470 ticks`
- `current_scalp`: `35` trades | `0` wins | `0` losses | `35` flats | `0 ticks`

Hedge `next_1`:

- `plan_created`: `536`
- `done`: `710` eventos contabilizados no paper
- `force_closed`: `468` eventos contabilizados no paper
- no recorte de ciclo classificado por plano criado: `527` casos, `59 done`, `468 force_closed`

## 4. Conclusoes por setup

### 4.1 `current_almost_resolved`

Desempenho:

- `289` trades
- win rate aproximada: `94.5%`
- media: `+1.626 ticks`

Contextos em que se comportou melhor:

- fase `tardia`
  - `65` trades
  - win rate `98.5%`
  - media `+2.585 ticks`
- `price to beat` alto
  - win rate `96.4%`
  - media `+2.012 ticks`
- entradas abaixo de `0.98`, especialmente quando ainda havia distancia util ate resolucao

Leitura estrategica:

- e um setup de continuacao madura, nao de descoberta de lado
- funciona bem em `alta` e `baixa`
- melhora quando o mercado esta perto do fim e ainda existe espaco real para capturar ticks
- aceita bem compressao no final e tambem suporta contexto de `reversao_ou_serrote` quando a dominancia ja esta clara

Resumo operacional:

- melhor setup do conjunto em qualidade
- especialmente forte para operacao manual no fim do candle

### 4.2 `next1_scalp`

Desempenho:

- `822` trades
- win rate aproximada: `67.4%`
- media: `+0.754 ticks`

Contextos em que se comportou melhor:

- tendencia `lateral`
  - `379` trades
  - win rate `76.0%`
  - media `+0.807`
- forca `fraca`
  - `586` trades
  - win rate `72.4%`
  - media `+0.823`
- `pullback_controlado`
  - menor frequencia
  - media `+1.056`
- lado `DOWN`
  - melhor que `UP`
  - media `+0.865` contra `+0.541`

Contextos piores:

- tendencia forte
- `tendencia_limpa` muito esticada
- ambientes em que o `next_1` ja correu demais

Leitura estrategica:

- o edge parece vir mais de atraso/descasamento do `next_1` do que de chase de impulso forte
- funciona melhor cedo, em contexto menos decidido
- e mais um setup de captura de lag do que um setup puro de continuacao limpa

Resumo operacional:

- melhor setup em volume
- bom para gerar oportunidades frequentes
- menos "bonito" que o `current_almost_resolved`, mas produtivo

### 4.3 `current_scalp`

Desempenho:

- `35` trades
- todos `flat`
- media `0.0`

Leitura estrategica:

- do jeito atual, nao demonstrou edge no paper
- o problema dominante nao parece ser regime de mercado
- antes de calibrar contexto, precisa revisar logica de saida, alvo e captura de movimento

Resumo operacional:

- setup sem valor comprovado no estado atual
- nao deve ser priorizado para rollout real antes de nova calibracao

### 4.4 Hedge `next_1` / fill-cycle

Desempenho estrutural:

- `527` planos classificados
- `done_rate`: `11.2%`
- `force_close_rate`: `88.8%`

Contextos relativamente melhores:

- `lateralizacao`
  - `done_rate 12.0%`
- `baixa`
  - `done_rate 14.3%`
  - amostra menor

Contextos piores:

- `alta`
  - `done_rate 6.7%`
- `tendencia_limpa`
  - `done_rate 6.1%`
- `pullback_controlado`
  - `done_rate 5.3%`

Leitura estrategica:

- confirma a tese de que o hedge se comporta melhor em mercado indeciso
- sofre em ambiente direcional limpo
- ainda depende demais de `force_close`, entao o ganho operacional precisa ser analisado com cuidado antes de considera-lo maduro financeiramente

Resumo operacional:

- setup compativel com lateralizacao
- ainda precisa endurecer a eficiencia do fechamento

## 5. Prioridade atual dos setups

Ordem sugerida, considerando evidencia empirica e maturidade operacional:

1. `current_almost_resolved`
2. `next1_scalp`
3. hedge `next_1`
4. `current_scalp`

## 6. Hipoteses estrategicas atuais

Hipoteses fortes:

- `current_almost_resolved` deve ser privilegiado no fim do candle, quando houver dominancia clara e distancia util restante
- `next1_scalp` deve ser privilegiado quando o `next_1` ainda esta cedo e o mercado esta mais lateral ou so levemente inclinado
- o hedge deve ser considerado ferramenta de mercado indeciso, nao de mercado ja decidido

Hipoteses fracas ou nao confirmadas:

- `current_scalp` ainda nao mostrou edge suficiente para ser priorizado
- a nova variante manual de quase resolvidos ainda precisa mais amostras para avaliacao isolada

## 7. Limitacoes da analise

- a parte mais forte da evidencia vem de paper, nao de historico real consolidado
- o hedge nao foi resumido em PnL por plano neste documento
- o `next1_scalp` dedicado nao gerou trades nesse recorte; a leitura dele veio do `all_setups`
- ainda nao existe neste repositorio um historico consolidado de ordens reais fechadas por setup e por regime

## 8. Proximos passos recomendados

- criar um analisador persistente por regime para reaproveitar em todos os proximos papers
- salvar em arquivo resumido:
  - win rate por setup
  - media de ticks por regime
  - motivos de saida por regime
- adicionar historico real consolidado quando houver logs de execucoes fechadas
- usar este documento como baseline para novas calibracoes

## 9. Regra de uso deste documento

Sempre que houver novo paper relevante ou nova rodada de execucao real:

1. preservar os logs no repositorio ou em storage versionado
2. recalcular os agregados por setup
3. atualizar este documento
4. evitar decisoes estrategicas baseadas apenas em memoria ou impressao visual

