# Almost Resolved Dynamic Distance

Este setup atualiza a captura passiva do quase resolvido para usar distancia dinamica contra o price to beat.

## Configuracao Atual

Entrada passiva:

```text
janela maxima: 90s
score minimo: 75
entrada: limite 1 tick abaixo do lider
stop: 3 ticks
hold: ate resolucao, salvo stop/protecao
```

Distancia minima:

```text
90s ate 61s:
  distancia >= max(100 USD, 3.0x volatilidade recente)

60s ate 31s:
  distancia >= max(70 USD, 2.5x volatilidade recente)

30s ate 1s:
  distancia >= max(50 USD, 2.0x volatilidade recente)
```

Travas que continuam ativas:

```text
leader >= 0.98
counter <= 0.03
distancia >= 10 bps
buffer minimo positivo contra reversao
reversao adversa 5s/15s/30s dentro do limite
range do book 30s dentro do limite
direcao precisa estar do lado vencedor do price to beat
```

## Simulacao em Log

Rodar replay dos tres modelos:

```powershell
python analyze_almost_resolved_time_extension_v1.py --dynamic --min-score 75 --output logs\research_base\almost_resolved_dynamic_distance_score75_v1.json
```

Modelos simulados:

```text
A fixo:
  90s >=100 USD, 60s >=70 USD, 30s >=50 USD

B volatilidade:
  90s >=3.0x vol, 60s >=2.5x vol, 30s >=2.0x vol

C hibrido:
  maior valor entre A e B
```

Resultado obtido nesta maquina com os logs consolidados:

```text
score >= 75
A fixo:    13 trades, 13 wins, 0 losses, +32.7 ticks
B vol:     13 trades, 13 wins, 0 losses, +32.7 ticks
C hibrido: 13 trades, 13 wins, 0 losses, +32.7 ticks

score >= 85
A fixo:     5 trades, 5 wins, 0 losses, +6.9 ticks
B vol:      5 trades, 5 wins, 0 losses, +6.9 ticks
C hibrido:  5 trades, 5 wins, 0 losses, +6.9 ticks
```

Mesmo quando o replay pegou as mesmas entradas, o modelo hibrido foi escolhido porque deve se comportar melhor fora da amostra: bloqueia mercado violento quando a distancia nominal parece boa, mas aceita distancia menor quando o mercado esta calmo e o tempo esta perto do fim.

## Rodar Paper

Paper recomendado para medir oportunidade e fills:

```powershell
python diagnostics_current_almost_resolved_paper_v1.py --seconds 0 --poll-secs 1 --order-qty 6 --passive-capture-only --hybrid-passive-to-aggressive --hybrid-aggressive-after-secs 2 --hybrid-aggressive-max-price 0.99 --hold-winner-to-resolution --resolution-settle-secs 1 --log-file logs\current_almost_resolved_dynamic_distance_paper_v1\dynamic_distance_live.jsonl
```

Esse paper usa os defaults atuais de `CurrentAlmostResolvedConfigV1`, portanto ja roda com distancia dinamica hibrida.

Paper completo antes do real, incluindo gray-zone e simulacao de fill mais conservadora:

```powershell
python diagnostics_current_almost_resolved_paper_v1.py --seconds 21600 --poll-secs 1 --order-qty 6 --hybrid-passive-to-aggressive --hybrid-aggressive-after-secs 2 --hybrid-aggressive-max-price 0.99 --passive-fill-touch-polls 2 --hold-winner-to-resolution --resolution-settle-secs 1 --enable-gray-zone --log-file logs\current_almost_resolved_full_setup_paper_v1\full_setup_live.jsonl
```

Esse modo nao usa `--passive-capture-only`: ele mede o setup completo. Cada snapshot vira uma decisao explicita: entrada por sinal forte, captura passiva, substituicao agressiva, gray-zone com alvo/stop curto, ou bloqueio por risco/executabilidade. O resumo final inclui `execution_funnel`, que separa sinais permitidos, ordens candidatas, ordens postadas, touches, substituicoes agressivas, skips e fills.

A substituicao agressiva no paper segue a mesma trava do real: somente para `passive_extreme_liquidity_capture`, com `secs_to_end <= 35`, ask dentro do cap, sem contexto de midpoint ausente, sem alerta de counter e com distancia segura confirmada. Use `stats.by_entry_order_style` no resumo para decidir se `aggressive_limit` gera ganho incremental suficiente; se nao gerar, mantenha apenas a passiva.

Para testar split de execucao apenas quando o vencedor esta claro e ficando extreme resolved, use:

```powershell
python diagnostics_current_almost_resolved_paper_v1.py --seconds 21600 --poll-secs 1 --order-qty 100 --hybrid-passive-to-aggressive --hybrid-aggressive-after-secs 2 --hybrid-aggressive-max-price 0.99 --passive-fill-touch-polls 2 --hold-winner-to-resolution --resolution-settle-secs 1 --enable-gray-zone --split-extreme-entry --split-aggressive-frac 0.5 --maker-rebate-bps 0 --log-file logs\current_almost_resolved_full_setup_paper_v1\full_setup_split_50_50_live.jsonl
```

Esse split so atua em `passive_extreme_liquidity_capture`, com `leader >= 0.98`, `counter <= 0.03`, `secs_to_end <= 45`, distancia segura, range baixo e ask dentro do cap. A metade agressiva entra imediatamente; a outra metade fica passiva 1 tick abaixo.

## Roadmap: Runner Go

Objetivo final: aproximar o bot do comportamento manual que vinha sendo positivo, sem deixar uma unica perda ruim devolver muitos ganhos pequenos.

Por enquanto, Python continua sendo a base correta para pesquisa:

```text
simulacao historica
papers
comparacao de setups
ajuste de parametros
diagnostico dos logs
```

Mais a frente, se os logs reais mostrarem que a perda de oportunidades vem principalmente da camada de execucao, faz sentido criar uma versao em Go somente para o runner real. Essa versao deve ser pequena, auditavel e focada em:

```text
ler mercado em tempo real
avaliar o setup quase resolvido completo
postar/cancelar/repostar ordens rapidamente
controlar stop, structural_stop e profit_protect
bloquear entradas quando a saida pode ficar ruim por falta de liquidez
registrar JSONL compativel com os analisadores atuais
```

A migracao nao deve trocar tudo de uma vez. Primeiro o runner Go roda em dry-run/paper, lado a lado com o Python, medindo:

```text
quantos sinais viram ordem
quantas ordens viram fill
quanto o fill real difere do paper
quanto slippage aparece nas saidas
quantas oportunidades manuais o bot ainda deixa passar
```

So depois dessa comparacao o Go deve receber permissao para ordens reais.

## Principio De Consistencia

O setup nao precisa acertar sempre. Loss faz parte do operacional. O ponto obrigatorio e impedir que uma unica perda devolva o lucro de muitas maos boas.

Regra do projeto:

```text
perda aceitavel = custo normal de operar
perda inaceitavel = evento que apaga o resultado de um dia ou semana
```

Antes de buscar mais fills, o runner precisa provar que consegue limitar o pior caso realista:

```text
tamanho pequeno e fixo na validacao
perda maxima por trade definida antes da entrada
limite de perda diaria
pausa apos slippage anormal
bloqueio quando a saida depender de book fino
comparacao entre stop teorico e stop pessimista
```

O criterio correto nao e simplesmente entrar mais. E entrar mais somente quando a perda ruim provavel continua proporcional ao ganho medio de uma ou poucas maos.

O paper registra `signal.planned_exit_risk` nos sinais liberados. Essa metrica estima:

```text
best_bid atual para saida
profundidade de bid observada
quantidade disponivel acima do stop
VWAP de saida para a mao planejada
perda teorica pelo stop
perda pessimista pela liquidez visivel
se existe profundidade suficiente para sair da quantidade planejada
```

Use essa leitura antes do real para identificar entradas em que o stop teorico parece pequeno, mas a saida realista pode ser muito pior por falta de book.

## Roadmap: Agente Parcialmente Autonomo

Um agente de IA pode ser util mais a frente, mas nao deve substituir primeiro as regras duras de execucao. A arquitetura mais segura e:

```text
runner deterministico: executa ordens, cancela, sai e respeita travas
agente: observa, resume, compara cenarios e sugere decisoes
politica dura/humano: autoriza o que pode virar ordem real
```

O agente pode analisar dados que hoje o script ainda trata de forma limitada:

```text
comportamento recente do book
distancia do price to beat
movimento do spot
micro tendencia
risco de reversao
padroes graficos parecidos com leitura manual
oportunidades que o manual pegaria e o bot perdeu
casos em que a saida ficou perigosa
```

Mas o agente nao pode ultrapassar estas travas:

```text
tamanho maximo
perda maxima por trade
perda maxima diaria
liquidez minima para saida
mercados permitidos
janelas de tempo permitidas
dados degradados bloqueiam entrada
sem aumentar posicao perdedora sem regra explicita
```

A analise grafica pode ajudar como filtro de contexto: continuidade de tendencia, exaustao, reversao provavel e qualidade do movimento. Primeiro ela deve entrar no paper como feature registrada em log, nao como permissao direta para ordem real.

Caminho recomendado:

```text
1. consolidar runner deterministico em paper e real supervisionado
2. medir fills, slippage, perdas ruins e oportunidades perdidas
3. adicionar features graficas/microestrutura ao log
4. testar agente apenas como consultor em historico e paper live
5. permitir que o agente sugira, mas nao execute
6. so considerar autonomia limitada depois que as travas duras forem confiaveis
```

## Painel

O painel manual mostra a regra dinamica em `Distancia do price to beat`, incluindo:

```text
tier atual: near, mid ou far
piso em USD
multiplicador da volatilidade
distancia minima calculada
```

Quando todos os criterios estiverem verdes, o painel mostra `entrada liberada`.
