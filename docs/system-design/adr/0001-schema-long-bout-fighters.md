# ADR 0001 -- Representação long/normalizada dos cantos em `bout_fighters`

- **Status**: aceita
- **Data**: 2026-07-10
- **Marco**: M0 -- Fundação (SPEC 002, Slice 01)
- **Contexto de decisão**: herdada como decisão load-bearing do `CLAUDE.md` e do PRD
  (seção 5.3); esta ADR a formaliza durante a implementação do schema.

## Contexto

O dataset histórico do Kaggle (`mdabbert/ultimate-ufc-dataset`) é **wide**: cada linha
representa uma luta com duas colunas por atributo (uma para o canto vermelho, outra para o
azul). Ao modelar o schema relacional, era preciso decidir como representar os dois cantos de
uma luta no Postgres, sabendo que o schema é **a decisão mais cara de reverter** -- tudo
(seed, API, prontidão preditiva) se amarra nele.

O requisito dominante do projeto é a **série temporal por lutador**: "histórico do lutador X
ao longo do tempo" e "head-to-head entre dois lutadores" são os cruzamentos centrais da API
(M2) e a fundação do modelo preditivo (fase 2, evolução/declínio de lutadores). As
estatísticas são um princípio inegociável: guardadas **por luta (granular)**, nunca como
médias globais; médias calculam-se on demand.

## Decisão

Modelar os cantos em **long/normalizado**: uma tabela `bout_fighters` com **uma linha por
lutador-por-luta**, unicidade `(bout_id, fighter_id)`, guardando as estatísticas granulares
daquele lutador naquela luta (knockdowns, significant strikes landed/attempted, takedowns
landed/attempted, submission attempts, control time em segundos) e o canto (`corner`:
red/blue). A tabela `bouts` guarda o que é da luta como um todo (evento, vencedor, método,
round, tempo de encerramento). Cada linha wide do CSV explode em **uma** linha de `bouts` e
**duas** de `bout_fighters` (uma por canto) no seed.

`bout_fighters.fighter_id` é indexado -- a query de histórico do lutador filtra por ele e
ordena cronologicamente pelo evento.

## Alternativas consideradas

- **Wide (espelhar o CSV)**: colunas `r_*`/`b_*` na própria `bouts` (duas colunas por
  atributo). **Preterida.** A query "histórico do lutador X" fica feia e cara: exige `OR`
  entre os dois lados (`r_fighter_id = X OR b_fighter_id = X`) e projeção condicional das
  stats do lado certo em cada linha -- exatamente o padrão que o preditivo exercitaria a todo
  momento. Facilitaria só o seed (mapeamento 1:1 com o CSV), que é justamente a parte barata e
  única.

## Consequências

**Positivas**
- Histórico do lutador ao longo do tempo é uma query trivial (`WHERE fighter_id = X`
  ordenado por data do evento), sem `OR` nos dois lados.
- Granularidade por luta preservada por construção; nenhuma média é pré-agregada de forma
  destrutiva. Base pronta para o preditivo (fase 2) sem reestruturação de schema.
- Head-to-head e agregações on demand se apoiam num modelo uniforme (uma linha por
  participação).

**Negativas / custos**
- O seed precisa **explodir** cada linha wide do CSV em duas linhas long, resolvendo o
  `fighter_id` de cada canto pela entity resolution (slice 02) e o `event_id` pelo seed de
  eventos (slice 03). Custo pago uma única vez, na carga.

## Decisões de schema correlatas (fechadas nesta slice)

Refinamentos de schema decididos junto com o model `Bout`, registrados aqui para rastreio
(não são ADRs próprias -- são detalhes da mesma decisão de modelagem):

- **Decisão #3 -- representação do tempo de encerramento da luta**: armazenado como
  **inteiro em segundos** (`ending_time_seconds`), não string `mm:ss`. Coerente com
  `control_time_seconds` (também segundos) e com a convenção de testes ("durações em segundos;
  asserte o valor numérico, não formatação"). A formatação `mm:ss` é responsabilidade de
  apresentação (M2), não do dado.
- **Decisão #4 -- empates e no contest**: `bouts.winner_id` é **nullable**. Um empate é
  `winner_id = NULL` com o `method` que o produziu (tipicamente `DECISION`); um no contest é
  `method = NO_CONTEST` com `winner_id = NULL`. **Não** se adiciona um valor `DRAW` ao enum
  `BoutMethod` -- o empate mora na nulabilidade do vencedor. Nesta slice o schema apenas
  **suporta** o caso; como o dataset marca esses registros é confirmado na Slice 04.

## Invariantes reforçadas

- Toda tabela tem coluna `source` (NOT NULL) -- rastreio de origem (`"kaggle"` no seed).
- Enums nativos do Postgres (`stance`, `bout_method`, `corner`); a migration inicial os cria
  no upgrade e os dropa explicitamente no downgrade (sem resíduo).
