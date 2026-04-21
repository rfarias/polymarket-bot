# README_ROADMAP_MULTI_REAL

Este documento organiza o caminho para levar os setups do repositório de estados mistos de pesquisa/paper/shadow para um modelo em que possam operar simultaneamente com ordens reais, de forma controlada.

## 1. Objetivo final

Estado desejado:

- varios setups ativos ao mesmo tempo
- todos usando a mesma conta real
- sem conflito entre ordens
- com ownership claro por setup
- com reconcile, persistencia e recovery consistentes
- com possibilidade de habilitar ou desabilitar cada setup individualmente

Hoje o repositório ainda nao esta nesse ponto. Alguns setups ja tem base operacional forte; outros ainda estao mais proximos de estrategia do que de infra de execucao real.

## 2. Mapa de maturidade atual

### 2.1 Mais maduro

- `next1 fill-cycle`
  - forte em reconcile
  - forte em startup guard
  - forte em persistencia
  - forte em fluxo real

- `next1 scalp`
  - runner real dedicado
  - state file proprio
  - restore e cleanup
  - preflight forte

### 2.2 Intermediario

- `current scalp`
  - logica de sinal madura
  - runner real dedicado criado
  - execucao ainda existe dentro do `multi-setup`
  - recovery inicial existe, mas ainda precisa endurecimento para coexistencia

- `current almost resolved`
  - logica de sinal clara
  - ja integrado ao `multi-setup`
  - ainda sem camada operacional dedicada

### 2.3 Menos pronto para simultaneidade

- `scalp reversal`
  - logica de entrada existe
  - state file proprio existe
  - ainda aborta se houver qualquer open order na conta
  - ainda nao convive bem com outros setups reais

## 3. Principio central para o modo simultaneo

O ponto principal nao e apenas "ter varios runners". O ponto principal e ter uma camada comum de ownership, reconcile e risco.

Se isso nao existir, vao aparecer estes problemas:

- um setup aborta porque encontrou ordem de outro setup
- um setup cancela ordem que nao e dele
- restart perde contexto de quem possui a posicao
- dois setups disputam o mesmo slot ou a mesma janela de liquidez
- o operador nao consegue saber qual setup gerou qual ordem

## 4. Requisitos minimos para qualquer setup entrar no modo multi-real

Antes de considerar um setup apto a rodar junto com os demais, ele precisa cumprir todos estes requisitos:

- preflight dedicado
- env vars dedicadas
- client keys padronizadas e unicas por setup
- startup guard que reconheca ownership das proprias ordens
- persistencia dedicada de estado
- restore/recovery no bootstrap
- logs de sessao e eventos
- regras claras de stop, timeout e cleanup

Se um setup nao cumprir isso, ele ainda nao deve entrar no pool simultaneo real.

## 5. Ordem recomendada de trabalho

## 5.1 Fase 1: consolidar os setups ja mais maduros

Objetivo:

- manter `next1 fill-cycle` como referencia de controle de risco
- manter `next1 scalp` como referencia de runner dedicado com state file

Trabalho:

1. Preservar a documentacao atual como fonte de verdade.
2. Padronizar nomenclatura de `client_order_key` entre os dois setups.
3. Garantir que ambos consigam reconhecer no bootstrap quais ordens pertencem a si mesmos.
4. Definir uma regra clara de prioridade entre eles se disputarem o mesmo slot `next_1`.

Gate de saida da fase:

- ambos os setups conseguem reiniciar sem confundir ordens um do outro
- ambos conseguem operar sem abortar por encontrar ordens validas do outro setup

## 5.2 Fase 2: endurecer `current scalp`

Objetivo:

- tirar `current scalp` da dependencia exclusiva do `multi-setup`
- criar um fluxo proprio de paper -> real -> coexistencia

Trabalho:

1. Criar diagnostico paper dedicado se necessario.
2. Endurecer o runner real dedicado de `current scalp`.
3. Consolidar state file proprio da posicao.
4. Fortalecer restore/recovery no bootstrap.
5. Padronizar `client_order_key` no formato do setup.
6. Adicionar startup guard por ownership, nao apenas bloqueio global.

Gate de saida da fase:

- `current scalp` roda sozinho em real
- consegue restaurar estado
- nao conflita com ordens de `next1 fill-cycle` e `next1 scalp`

## 5.3 Fase 3: promover `current almost resolved`

Objetivo:

- transformar o fallback do `current` em setup operacional explicito

Trabalho:

1. Decidir se ele tera runner proprio ou continuara como modulo do `multi-setup`.
2. Se tiver runner proprio:
   - criar preflight
   - criar persistencia
   - criar restore/recovery
3. Se continuar no `multi-setup`:
   - explicitar ownership da posicao
   - salvar estado separado do `current scalp`
   - tornar bootstrap previsivel

Gate de saida da fase:

- `current almost resolved` tem caminho operacional deterministico
- o operador consegue distinguir sua posicao e suas ordens das do `current scalp`

## 5.4 Fase 4: adaptar `scalp reversal` para coexistencia

Objetivo:

- remover a limitacao de "nao inicia se houver qualquer open order"

Trabalho:

1. Trocar o bootstrap atual por startup guard orientado a ownership.
2. Definir prefixo claro de `client_order_key` para o setup.
3. Fazer o runner ignorar ordens abertas de outros setups quando apropriado.
4. Criar reconcile mais rico com status do broker.
5. Rever se ele deve operar no `current`, no `next_1`, ou nos dois ao mesmo tempo.

Gate de saida da fase:

- `scalp reversal` consegue iniciar em uma conta que ja tenha ordens de outros setups
- sem cancelar ou assumir ordens que nao sao dele

## 5.5 Fase 5: coordenador comum de multi-real

Objetivo:

- sair de varios runners independentes para um modelo coordenado

Esse coordenador deve centralizar:

- registro dos setups habilitados
- ownership de ordens por setup
- tabela de client key prefixes
- startup guard global
- reconcile global
- limite de exposicao por setup
- limite de exposicao total da conta
- arbitragem de prioridade entre setups concorrentes

Gate de saida da fase:

- todos os setups podem ser iniciados por um ponto unico de controle
- cada setup continua isolado em sua propria logica
- risco e ownership ficam centralizados

## 6. O que falta tecnicamente em cada setup

## 6.1 Next1 fill-cycle

Falta principal:

- regra explicita de coexistencia com outros setups de `next_1`
- ownership mais padronizado de ordens
- coordenacao superior para nao disputar slot com `next1 scalp`

## 6.2 Next1 scalp

Falta principal:

- integracao com ownership global de ordens
- regra de arbitragem com `next1 fill-cycle`
- eventualmente consolidar logs/estado num padrao compartilhado

## 6.3 Current scalp

Falta principal:

- runner real dedicado
- state file proprio
- restore/recovery
- startup guard proprio por ownership

## 6.4 Current almost resolved

Falta principal:

- identidade operacional propria
- persistencia propria
- lifecycle claro fora da logica ad hoc do `multi-setup`

## 6.5 Scalp reversal

Falta principal:

- coexistencia com outras ordens abertas
- reconcile e ownership padronizados
- decisao de escopo final de slots

## 7. Roadmap minimo por setup

Se a estrategia for evoluir um setup por vez, a ordem recomendada e:

1. endurecer `current scalp`
2. `current almost resolved`
3. `scalp reversal`

Motivo:

- `current scalp` ja esta mais perto do padrao desejado
- `current almost resolved` aproveita parte do mesmo contexto do `current`
- `scalp reversal` ainda precisa de mais retrabalho estrutural para coexistencia

## 8. Definicao de pronto por etapa

### 8.1 Pronto para paper

- sinal documentado
- diagnostico paper funcional
- logs suficientes para entender entrada e saida

### 8.2 Pronto para shadow

- entra no loop real sem postar ordens
- registra decisao, preco teorico, alvo, stop e timeout
- nao perde contexto operacional

### 8.3 Pronto para real dedicado

- preflight forte
- startup guard ou equivalente
- ownership de ordens
- persistencia
- restore/recovery

### 8.4 Pronto para multi-real

- convive com ordens de outros setups
- respeita ownership de ordens
- respeita limites de risco compartilhados
- participa de arbitragem de prioridade

## 9. Regras de prioridade sugeridas

Enquanto nao existir coordenador global sofisticado, a prioridade mais segura e:

1. `next1 fill-cycle`
2. `next1 scalp`
3. `current scalp`
4. `current almost resolved`
5. `scalp reversal`

Racional:

- o fill-cycle hedgeado e o mais maduro em reconcile e controle
- o `next1 scalp` ja tem runner real dedicado e state restore
- os setups do `current` ainda precisam endurecimento operacional
- `scalp reversal` hoje e o mais fraco para coexistencia

## 10. Plano de execucao recomendado

Se o objetivo for realmente chegar ao modo simultaneo real, a sequencia mais eficiente e:

1. Congelar naming e ownership das ordens.
2. Endurecer o runner real dedicado para `current scalp`.
3. Dar identidade operacional propria ao `current almost resolved`.
4. Refatorar `scalp reversal` para startup guard por ownership.
5. So depois construir o coordenador global multi-real.

Isso evita tentar resolver simultaneidade antes de cada setup ter lifecycle confiavel sozinho.

## 11. Proximo passo mais util no codigo

O proximo passo de maior retorno tecnico e:

- endurecer o runner real dedicado do `current scalp`

Motivo:

- ja existe sinal maduro
- o runner dedicado ja existe e virou o melhor ponto de iteracao
- ele continua sendo o melhor candidato para virar o terceiro setup real estavel do projeto

Depois disso:

- separar `current almost resolved`
- endurecer `scalp reversal`
- iniciar a camada coordenadora multi-real
