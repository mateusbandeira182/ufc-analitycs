# Capturas verbatim da API oficial da UFC (SPEC 008, Sprint 008-01)

Os JSON deste diretório são **respostas HTTP cruas**, baixadas com `curl -o`, sem `jq`,
sem pretty-print e sem nenhuma edição manual. São o **contrato de verdade** dos endpoints da
fonte oficial: se um deles for podado, reformatado ou reconstruído a partir do DTO, o valor de
teste evapora -- foi exatamente uma fixture "limpa" que escondeu a divergência `sigStrikes` do
M5 por meses (ver lição `licao-fixture-unica-e-contrato-de-verdade`).

O teste `tests/ingestion/test_ufc_official_contract_real_payload.py` inclui um **teste-guarda de
verbatimidade** que falha se a captura passar a ter apenas as chaves que o DTO declara.

- **Base URL**: `https://d29dxerjsp82wz.cloudfront.net`
- **Momento da captura**: 2026-09-01T19:19:47Z para as quatro primeiras (mesma execução); as
  capturas acrescentadas depois trazem o próprio momento na seção que as descreve
- **Autenticação**: nenhuma. Sem chave, sem header, sem quota. **Não é a Cito** -- nenhuma
  captura deste diretório consumiu o free tier de 500 req/mês.
- **Nenhum segredo**: as respostas são públicas e não têm credencial no corpo.

## Arquivos

| Arquivo | URL | HTTP | Bytes | Papel |
|---|---|---|---|---|
| `event_live_1124.json` | `/api/v3/event/live/1124.json` | 200 | 67985 | Captura principal do endpoint de evento |
| `fight_live_10214.json` | `/api/v3/fight/live/10214.json` | 200 | 18104 | Captura principal do endpoint de luta |
| `event_live_700.json` | `/api/v3/event/live/700.json` | 200 | 46943 | Prova que a data de calendário é a **local**, não a UTC |
| `event_live_1335.json` | `/api/v3/event/live/1335.json` | 200 | 28523 | Prova que o canto existe **antes** do desfecho |
| `event_live_1345.json` | `/api/v3/event/live/1345.json` | 200 | 22 | Prova que id inexistente é **200 com envelope vazio**, não 404 |
| `event_live_1002.json` | `/api/v3/event/live/1002.json` | 200 | 70329 | Um dos dois `Bruno Silva` (Sprint 008-03) e o `MMAId` **ausente** |
| `event_live_1073.json` | `/api/v3/event/live/1073.json` | 200 | 71487 | O **outro** `Bruno Silva` (Sprint 008-03) |
| `event_live_566.json` | `/api/v3/event/live/566.json` | 200 | 27852 | Rótulo de `Stance` fora do nosso enum (Sprint 008-05) |
| `fight_live_12204.json` | `/api/v3/fight/live/12204.json` | 200 | 26619 | Granular completo, 5 rounds (Sprint 008-07) |
| `fight_live_13017.json` | `/api/v3/fight/live/13017.json` | 200 | 2621 | Granular **presente e vazio** em luta futura (Sprint 008-07) |

### `event_live_1124.json` -- UFC 282: Blachowicz vs. Ankalaev

- `EventId` 1124, `StartTime` `2022-12-10T23:30Z`, `TimeZone` `GMT-08:00`, `Status` `Final`.
- **Data do evento**: 2022-12-10 (local e UTC coincidem neste caso).
- **Dentro da janela** da RF-03 (>= 2010-03-21) e **presente na nossa base**: `events.id = 117`,
  `name = "UFC 282: Blachowicz vs. Ankalaev"`, `date = 2022-12-10`, `source = "kaggle"`.
- 12 lutas, 24 lutadores.

**Lutas com vencedor no canto AZUL** (o dado que sustenta o CA-03 -- sem isso a distinção entre
`Corner` e `Outcome` ficaria apenas afirmada, nunca demonstrada):

| `FightId` | Canto vermelho | Canto azul | Vencedor |
|---|---|---|---|
| 10214 | Darren Till (`Loss`) | **Dricus Du Plessis (`Win`)** | azul |
| 10227 | Bryce Mitchell (`Loss`) | **Ilia Topuria (`Win`)** | azul |

A luta principal (`FightId` 10211, Blachowicz x Ankalaev) terminou em **`Draw`** nos dois cantos
-- terceiro valor possível de `Outcome`, que por si só falsifica qualquer leitura de `Outcome`
como booleano derivado do canto.

### `fight_live_10214.json` -- Till vs. Du Plessis (a mesma luta de canto azul vencedor)

- `FightId` 10214, `Status` `Final`, `WeightClass` Middleweight.
- Canto vermelho: Darren Till, `FighterId` 2544, `Outcome` `Loss`, `Height` 72.0, **`Reach`
  74.5** (meia polegada -- o valor que prova que a antropometria chega **em polegadas, sem
  conversão**, e que arredondar para inteiro perderia informação), `Weight` 185.0,
  `DOB` `1992-12-24`, `Stance` `Southpaw`.
- Canto azul: Dricus Du Plessis, `FighterId` 3599, `Outcome` `Win`, `Height` 73.0, `Reach` 76.0,
  `Weight` 185.0, `DOB` `1994-01-14`, `Stance` `Switch`.

O endpoint de luta repete a **mesma forma** de `Fighters[]` do card do evento (medido: conjunto
de chaves idêntico), mas o objeto de luta difere: ele **acrescenta** `Event`, `OfficialStats`,
`FightStats` e `RoundStats`, e **não tem** `FightNightTracking`. O bloco `Event` do endpoint de
luta é **reduzido** -- não traz `Organization` nem `FightCard` --, então não é o mesmo objeto do
endpoint de evento e não foi modelado por um DTO compartilhado.

`FightStats`/`RoundStats` **não** são declarados por `UfcOfficialFight` (o DTO da Sprint 008-01):
ficam preservados na captura e são parte do que o teste-guarda de verbatimidade usa como prova de
que a fixture é crua. A Sprint 008-07 os declarou num DTO **separado**
(`UfcOfficialFightGranular`), consumido só pela medição do portão -- ver
`fight_live_12204.json` abaixo.

### `event_live_700.json` -- UFC 184: Rousey vs Zingano (captura de apoio)

Capturada porque resolve, **com evidência versionada**, uma decisão de borda que o plano da
sprint delegou explicitamente à captura ("se a data vier como instante ISO com fuso,
`event_date: date` não parseia"):

- `StartTime` é `2015-03-01T00:00Z` -- a **data UTC é 2015-03-01**.
- `TimeZone` é `GMT-08:00`, logo o instante local é `2015-02-28T16:00`.
- A nossa base registra `events.id = 438`, `date = 2015-02-28`.

Ou seja: **a data de calendário do evento é a local, não a UTC**, e usar a UTC erraria o dia em
eventos noturnos das Américas. É a mesma armadilha já medida na Cito (`startsAt` UTC divergindo
de `eventDate` local, ADR 0005). O DTO expõe o instante (`start_time`) e deriva `local_date`
aplicando o deslocamento de `TimeZone`.

Este evento também tem vencedor no canto azul (`FightId` 5188, Holm `Win` sobre Pennington).

### `event_live_1335.json` -- UFC 331: Van vs. Pantoja 2 (captura de apoio)

Evento com `Status` `Upcoming` (2026-09-19, futuro em relação à captura). Resolve a segunda
decisão de borda delegada à captura -- a nulabilidade de `Outcome` -- e é a **prova mais forte
do CA-03**:

- Todos os lutadores têm `Corner` preenchido (`"Red"`/`"Blue"`).
- Todos têm `Outcome` como objeto **presente** com os dois campos **nulos**:
  `{"OutcomeId": null, "Outcome": null}`.

O canto existe antes de a luta acontecer; o desfecho não. Um campo não pode ser derivado do
outro. Por isso `outcome_id` e `label` são opcionais no DTO enquanto o objeto `Outcome` em si é
estrutural, e por isso `corner` é obrigatório.

### `event_live_1345.json` -- id inexistente (captura da Sprint 008-02)

Capturada em 2026-09-01T19:55:58Z, quando a varredura da Slice 02 precisou distinguir "id que
não existe" de "a fonte mudou de contrato". O corpo inteiro é `{"LiveEventDetail":{}}` -- 22
bytes, **HTTP 200**.

A fonte **não responde 404** para id inexistente. Medido nos ids 0, 1344, 1345, 1350, 1400,
2000, 5000 e 99999: todos 200 com o envelope vazio, enquanto o id 1343 (`UFC Fight Night:
Bonfim vs. Brady`) ainda era um evento real. Sem tratar esse caso, a varredura abortaria no
primeiro id acima da fronteira como se a fonte tivesse quebrado o contrato -- por isso
`ingestion.ufc_official.dto.is_absent_event_payload` existe e é consultado **antes** da
validação.

A distinção é estrita: só o objeto rigorosamente vazio é ausência. Um `LiveEventDetail`
parcialmente preenchido continua sendo quebra de contrato e falha alto (RF-10).

### `event_live_1002.json` e `event_live_1073.json` -- os dois `Bruno Silva` (Sprint 008-03)

Capturadas em 2026-09-01T21:37:50Z, com `curl -o`, quando a Slice 03 precisou provar que o
homônimo resolve pelo **contexto da luta** e não pelo nome:

- `1002` -- UFC Fight Night: Moraes vs. Sandhagen (2020-10-10). Traz `Bruno Silva`
  **"Bulldog"**, `FighterId` 3283, `MMAId` 146112, `DOB` 1990-03-16, contra Tagir Ulanbekov.
- `1073` -- UFC Fight Night: Santos vs. Ankalaev (2022-03-12). Traz `Bruno Silva`
  **"Blindado"**, `FighterId` 3314, `MMAId` 160594, `DOB` 1989-07-13, contra Alex Pereira.

As duas datas de nascimento batem **exatamente** com as dos dois `Bruno Silva` persistidos
(`fighters.id` 1638 e 2204), então o cenário do teste é o caso real, não uma construção.

A `1002` acumula um segundo papel: KB Bhullar (`FighterId` 3567) vem com **`MMAId` nulo** --
1 de 26 cantos do card --, e é sobre ela que o teste de "campo ausente permanece nulo" roda.
O conjunto **derivado** que cobre os outros sete homônimos vive em `../ufc_official_homonimos/`.

### `event_live_566.json` -- UFC 140: Jones vs Machida, o `Stance` fora do enum (Sprint 008-05)

Capturada em 2026-09-01, com `curl -o`, quando a Slice 05 mediu os rótulos de `Stance` que a
fonte publica e descobriu que são **cinco**, não três:

| Rótulo | Cantos | Lutadores distintos |
|---|---|---|
| `Orthodox` | 17.008 | -- |
| `Southpaw` | 4.371 | -- |
| `Switch` | 1.242 | -- |
| `Open Stance` | 30 | 7 |
| `Sideways` | 6 | 3 |
| `null` | 987 | -- |

(Medido sobre 23.644 cantos dos 1.354 payloads do cache local, em 2026-09-01.)

`Open Stance` e `Sideways` **não** têm representação em `apps.fighters.enums.Stance`, e
`ingestion.ufc_official.anthropometry.parse_stance` levanta `UnknownStanceError` diante deles
(RF-10). Sem esta captura, o caminho de falha alta ficaria exercitado só por um rótulo inventado
-- e um rótulo inventado não prova que o caminho é necessário.

- `EventId` 566, `StartTime` `2011-12-10T22:30Z`, `TimeZone` `GMT-05:00`, `Status` `Final`,
  `Organization` 1 (UFC). **Dentro da janela** da RF-03.
- Krzysztof Soszynski (`FighterId` 309, canto vermelho) vem com `Stance` `"Open Stance"`,
  `Height` 73.0, `Reach` 77.5, `Weight` 205.0, `DOB` `1977-08-02`. O adversário, Igor Pokrajac
  (`FighterId` 966), é `Orthodox` -- é o par que prova que o rótulo de um canto não contamina o
  outro.

A captura sustenta também a decisão de **alcance do estrago**: o rótulo não mapeado zera só a
base do lutador, e os outros quatro atributos dele continuam sendo preenchidos.

### `fight_live_12204.json` e `fight_live_13017.json` -- o granular (Sprint 008-07)

Capturadas em 2026-09-01T21:39:12Z e 2026-09-01T21:40:17Z, com `curl -o`, para o portão da
Slice 07 -- a medição que decide se a fonte oficial substitui a Cito no round-a-round.

- `12204` -- **Imavov x Borralho**, a luta principal de `UFC Fight Night: Imavov vs. Borralho`
  (`EventId` 1271, 2025-09-06), que é o **evento de referência** da medição. `Status` `Final`,
  cinco rounds completos nos dois cantos. `FightStats` traz 2 linhas (uma por canto) e
  `RoundStats` traz 2 entradas de `{FighterId, Rounds: [...]}` com 5 rounds cada. Imavov
  (`FighterId` 3576, canto **vermelho**, `Win`) tem `SigStrikesLanded` 81, `ControlTime`
  `"0:29"`; Borralho (`FighterId` 3658, canto **azul**, `Loss`) tem 66 e `"1:20"`.
- `13017` -- luta ainda **não realizada** do UFC 331 (2026-09-19). `FightStats` e `RoundStats`
  vêm **presentes e vazios** (`[]`). É a prova de que as duas chaves são **estruturais** -- um
  rename estoura (RF-10) -- e de que a lista vazia é a forma legítima de dizer "esta luta ainda
  não aconteceu". Sem esta captura, a distinção entre "vazio" e "ausente" ficaria afirmada e não
  demonstrada.

**Forma medida do granular** (78 linhas de estatística das 13 lutas do evento 1271, mais a luta
futura, sondagem de 2026-09-01): os 22 campos que `bout_fighter_rounds` guarda vieram
**presentes e não nulos em 78 de 78** linhas, todos `int`, exceto `ControlTime`, que é `str` no
formato `"m:ss"`. É essa medição que sustenta a decisão de declará-los **obrigatórios** no DTO:
se fossem opcionais, um rename degradaria para `None`, a linha sairia do denominador da medição
pela regra "ausência não é divergência" e o relatório informaria 100% sobre um campo que deixou
de existir.

A fonte devolve ~65 campos por linha de estatística (tempo por posição, acurácias, controle em
sete recortes de posição); o DTO declara 22 e o resto fica preservado na captura -- é parte do
que o teste-guarda de verbatimidade usa como prova de que a fixture é crua.

## Forma medida (24 eventos, 450 lutadores, sondagem de 2026-09-01)

A sondagem cobriu os ids 1, 25, 60, 120, 200, 300, 400, 500, 600, 700, 750, 800, 850, 900, 950,
1000, 1050, 1100, 1124, 1150, 1200, 1250, 1300, 1319 e serve de base para decidir o que é
obrigatório e o que é opcional no DTO:

| Campo | Tipo JSON observado | Nulos |
|---|---|---|
| `EventId`, `FightId`, `FightOrder`, `FighterId` | `int` | 0 |
| `Name` (evento), `StartTime`, `TimeZone`, `Status` | `str` | 0 |
| `FightCard`, `Fighters` | `list` | 0 |
| `Name` (lutador) | `dict` (`FirstName`/`LastName`/`NickName`) | 0 |
| `Corner` | `str` (`"Red"` / `"Blue"`, 225 x 225) | 0 |
| `Outcome` | `dict` (`OutcomeId`/`Outcome`) | 0 (mas com campos internos nulos em `Upcoming`) |
| `Weight`, `Height` | `float` (nunca `int`) | 0 |
| `Reach` | `float` | 50 de 450 |
| `DOB` | `str` ISO (`AAAA-MM-DD`) | 8 de 450 |
| `Stance` | `str` (`Orthodox`, `Southpaw`, `Switch`) | 2 de 450 |
| `MMAId` | `int` | 69 de 450 |

`TimeZone` veio no formato `GMT±HH:MM` em 24 de 24 eventos. Valores de `Outcome.Outcome`
observados: `Win`, `Loss`, `Draw`, `No Contest` -- e `null` em evento `Upcoming`.

A varredura mostrou também que a fonte cobre outras promoções (WEC, DREAM, K-1, CWFC, DWCS,
Road to UFC) além do UFC. O escopo do projeto segue **só UFC**. A Slice 02 resolveu a filtragem
pelo campo `Organization` do próprio payload (`OrganizationId` 1 = UFC, 2 = PRIDE, 3 = WEC,
4 = Strikeforce, 8 = DREAM, 9 = K-1, 67 = DWCS, 68 = Road to UFC), medido presente em 85 de 85
eventos sondados -- por isso `Organization` passou a ser campo estrutural do DTO.

## Como recapturar

```bash
BASE=https://d29dxerjsp82wz.cloudfront.net
DEST=tests/ingestion/fixtures/ufc_official
curl -sS -o "$DEST/event_live_1124.json" "$BASE/api/v3/event/live/1124.json"
curl -sS -o "$DEST/fight_live_10214.json" "$BASE/api/v3/fight/live/10214.json"
curl -sS -o "$DEST/event_live_700.json"  "$BASE/api/v3/event/live/700.json"
curl -sS -o "$DEST/event_live_1335.json" "$BASE/api/v3/event/live/1335.json"
curl -sS -o "$DEST/event_live_1345.json" "$BASE/api/v3/event/live/1345.json"
curl -sS -o "$DEST/event_live_1002.json" "$BASE/api/v3/event/live/1002.json"
curl -sS -o "$DEST/event_live_1073.json" "$BASE/api/v3/event/live/1073.json"
curl -sS -o "$DEST/event_live_566.json"  "$BASE/api/v3/event/live/566.json"
curl -sS -o "$DEST/fight_live_12204.json" "$BASE/api/v3/fight/live/12204.json"
curl -sS -o "$DEST/fight_live_13017.json" "$BASE/api/v3/fight/live/13017.json"
```

Nunca passe a captura por `jq`, por formatador ou por um `json.dump` do Python: os arquivos são
minificados numa única linha, exatamente como o CDN os devolveu.
