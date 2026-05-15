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

## Painel

O painel manual mostra a regra dinamica em `Distancia do price to beat`, incluindo:

```text
tier atual: near, mid ou far
piso em USD
multiplicador da volatilidade
distancia minima calculada
```

Quando todos os criterios estiverem verdes, o painel mostra `entrada liberada`.
