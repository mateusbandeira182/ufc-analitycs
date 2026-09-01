# Capturas verbatim da API oficial da UFC (SPEC 008, Sprint 008-01)

Os quatro JSON deste diretório são **respostas HTTP cruas**, baixadas com `curl -o`, sem `jq`,
sem pretty-print e sem nenhuma edição manual. São o **contrato de verdade** dos endpoints da
fonte oficial: se um deles for podado, reformatado ou reconstruído a partir do DTO, o valor de
teste evapora -- foi exatamente uma fixture "limpa" que escondeu a divergência `sigStrikes` do
M5 por meses (ver lição `licao-fixture-unica-e-contrato-de-verdade`).

O teste `tests/ingestion/test_ufc_official_contract_real_payload.py` inclui um **teste-guarda de
verbatimidade** que falha se a captura passar a ter apenas as chaves que o DTO declara.

- **Base URL**: `https://d29dxerjsp82wz.cloudfront.net`
- **Momento da captura**: 2026-09-01T19:19:47Z (todas as quatro, na mesma execução)
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

`FightStats`/`RoundStats` são o portão condicional da Slice 07 e **não** são declarados no DTO
desta slice; ficam preservados na captura e são justamente parte do que o teste-guarda de
verbatimidade usa como prova de que a fixture é crua.

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
Road to UFC) além do UFC. O escopo do projeto segue **só UFC**; a filtragem é problema da
Slice 02, não do DTO.

## Como recapturar

```bash
BASE=https://d29dxerjsp82wz.cloudfront.net
DEST=tests/ingestion/fixtures/ufc_official
curl -sS -o "$DEST/event_live_1124.json" "$BASE/api/v3/event/live/1124.json"
curl -sS -o "$DEST/fight_live_10214.json" "$BASE/api/v3/fight/live/10214.json"
curl -sS -o "$DEST/event_live_700.json"  "$BASE/api/v3/event/live/700.json"
curl -sS -o "$DEST/event_live_1335.json" "$BASE/api/v3/event/live/1335.json"
```

Nunca passe a captura por `jq`, por formatador ou por um `json.dump` do Python: os arquivos são
minificados numa única linha, exatamente como o CDN os devolveu.
