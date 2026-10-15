import type {
  BoutCardOut,
  EventDetailOut,
  EventHighlightsOut,
  EventOut,
  EventSummaryOut,
} from "@/api/schema";
import { todayIsoDate } from "@/features/events/syncStatus";

/*
  Fixtures de eventos para os testes e handlers MSW. Espelham o shape real de
  EventOut / EventDetailOut (contrato do M2, estendido na Slice 07 com `summary`,
  `highlights` e os campos de card em `BoutCardOut`).

  A partição `past`/`upcoming` é declarada aqui, em dois arrays disjuntos: o
  backend a deriva do `status` da luta (não da data), e um mock que tentasse
  re-derivar a regra duplicaria a lógica sem provar nada. O handler só filtra.
*/

/** Eventos realizados nomeados, usados pelas asserções de conteúdo. */
const PAST_NAMED_EVENTS: EventOut[] = [
  {
    id: 42,
    name: "UFC 300",
    date: "2024-04-13",
    location: "Las Vegas, USA",
    source: "kaggle",
  },
  {
    id: 41,
    name: "UFC 299",
    date: "2024-03-09",
    location: "Miami, USA",
    source: "kaggle",
  },
  {
    id: 40,
    name: "UFC Fight Night: Ribas vs. Namajunas",
    date: "2024-03-23",
    location: null,
    source: "kaggle",
  },
];

/*
  Enchimento do acervo realizado: a lista de Eventos pagina em
  EVENTS_SECTION_PAGE_SIZE (10) por seção, e os testes de paginação independente
  precisam de mais de uma página em "Realizados" sem que isso diga nada sobre
  "Próximos". Vinte e dois eventos aqui somam vinte e cinco com os nomeados, ou
  três páginas.
*/
const PAST_FILLER_EVENTS: EventOut[] = Array.from(
  { length: 22 },
  (_, index): EventOut => ({
    id: 300 + index,
    name: `UFC ${String(298 - index)}`,
    date: `2023-${String(12 - (index % 12)).padStart(2, "0")}-05`,
    location: "Las Vegas, USA",
    source: "kaggle",
  }),
);

/** Acervo realizado (`when=past`): 25 eventos, mais recentes primeiro. */
export const PAST_EVENT_FIXTURES: EventOut[] = [
  ...PAST_NAMED_EVENTS,
  ...PAST_FILLER_EVENTS,
];

/**
 * Data a `days` do dia de hoje, como `YYYY-MM-DD`. As fixtures de `upcoming`
 * precisam de datas **relativas**: uma data fixa no futuro vira passado com o
 * tempo e inverteria o marcador "aguardando sincronização" sem ninguém mexer
 * no teste — o mesmo apodrecimento do teste com id de evento embutido.
 */
function isoDateFromToday(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return todayIsoDate(d);
}

/** Data do `UFC 332`: já passada, com o card inteiro ainda agendado (caso da DT-1). */
export const AWAITING_SYNC_EVENT_DATE = isoDateFromToday(-12);

/*
  Acervo por vir (`when=upcoming`), em ordem crescente de data, como o backend
  entrega. O primeiro é o caso medido da DT-1: data **já passada** com o card
  inteiro ainda `scheduled`, porque o `gap_sync` é manual. Ele aparece em
  "Próximos" com o marcador "aguardando sincronização". O último é o `UFC 335`,
  no acervo **sem nenhuma luta**.
*/
export const UPCOMING_EVENT_FIXTURES: EventOut[] = [
  {
    id: 801,
    name: "UFC 332",
    date: AWAITING_SYNC_EVENT_DATE,
    location: "Las Vegas, USA",
    source: "cito",
  },
  {
    id: 812,
    name: "UFC 334",
    date: isoDateFromToday(23),
    location: "Abu Dhabi, UAE",
    source: "cito",
  },
  {
    id: 813,
    name: "UFC 335",
    date: isoDateFromToday(58),
    location: null,
    source: "cito",
  },
];

/** Acervo inteiro (sem `when`): a união das duas partições, que são disjuntas. */
export const EVENT_FIXTURES: EventOut[] = [
  ...PAST_EVENT_FIXTURES,
  ...UPCOMING_EVENT_FIXTURES,
];

/*
  Card do UFC 300 (id 42): três lutas em duas seções, na ordem que o backend
  entrega (`bout_order asc nulls_last, id asc`). `bout_order` traz o valor real
  da Cito — 1001 na luta principal, que é o MENOR valor, nem sequencial nem
  denso. Só um lutador tem `headshot_url`: o retrato cobre 794 dos 2.754
  lutadores, então o fallback por iniciais é o caso comum.
*/
const UFC_300_BOUTS: BoutCardOut[] = [
  {
    id: 7,
    winner_id: 100,
    method: "ko_tko",
    round: 2,
    ending_time_seconds: 225,
    weight_class: "Light Heavyweight",
    source: "kaggle",
    status: "completed",
    card_section: "Main Card",
    bout_order: 1001,
    fighters: [
      {
        fighter_id: 100,
        name: "Alex Pereira",
        corner: "red",
        headshot_url: "https://example.test/pereira.png",
      },
      {
        fighter_id: 200,
        name: "Jamahal Hill",
        corner: "blue",
        headshot_url: null,
      },
    ],
  },
  {
    id: 8,
    winner_id: null,
    method: "no_contest",
    round: null,
    ending_time_seconds: null,
    weight_class: null,
    source: "kaggle",
    status: "completed",
    card_section: "Main Card",
    bout_order: 1002,
    fighters: [
      {
        fighter_id: 300,
        name: "Charles Oliveira",
        corner: "red",
        headshot_url: null,
      },
      {
        fighter_id: 400,
        name: "Arman Tsarukyan",
        corner: "blue",
        headshot_url: null,
      },
    ],
  },
  {
    id: 9,
    winner_id: 500,
    method: "submission",
    round: 1,
    ending_time_seconds: 230,
    weight_class: "Middleweight",
    source: "kaggle",
    status: "completed",
    card_section: "Prelims",
    bout_order: 2001,
    fighters: [
      { fighter_id: 500, name: "Bo Nickal", corner: "red", headshot_url: null },
      {
        fighter_id: 600,
        name: "Cody Brundage",
        corner: "blue",
        headshot_url: null,
      },
    ],
  },
];

/** Resumo do UFC 300: os seis baldes somam `n_bouts` (3), com o denominador declarado. */
const UFC_300_SUMMARY: EventSummaryOut = {
  n_bouts: 3,
  ko_tko: 1,
  submissions: 1,
  decisions: 0,
  draws: 0,
  no_contests: 1,
  dq: 0,
};

/*
  Destaques CALCULADOS do UFC 300. `fastest_finish` carrega o par
  `(round, ending_time_seconds)` porque o tempo é **dentro** do round — não há
  total decorrido no contrato. `biggest_upset` vem do registro persistido em
  `bout_predictions` e declara seu próprio denominador (2 de 3 lutas previstas).
*/
const UFC_300_HIGHLIGHTS: EventHighlightsOut = {
  fastest_finish: {
    bout_id: 9,
    fighter: { id: 500, name: "Bo Nickal" },
    method: "submission",
    round: 1,
    ending_time_seconds: 230,
  },
  most_sig_strikes: {
    bout_id: 7,
    fighter: { id: 100, name: "Alex Pereira" },
    value: 42,
  },
  most_takedowns: {
    bout_id: 9,
    fighter: { id: 500, name: "Bo Nickal" },
    value: 3,
  },
  biggest_upset: {
    bout_id: 9,
    winner: { id: 500, name: "Bo Nickal" },
    prob_of_winner: 0.28,
    n_bouts_with_prediction: 2,
    n_bouts: 3,
  },
};

/*
  Card do UFC 332 (id 801): data já passada e as duas lutas ainda `scheduled`,
  sem `card_section` nem `bout_order` (o contexto da Cito só cobre evento
  recente e de forma irregular). `summary` e `highlights` chegam nulos — não há
  luta realizada classificável, e zero não é a resposta.
*/
const UFC_332_BOUTS: BoutCardOut[] = [
  {
    id: 90,
    winner_id: null,
    method: null,
    round: null,
    ending_time_seconds: null,
    weight_class: "Welterweight",
    source: "cito",
    status: "scheduled",
    card_section: null,
    bout_order: null,
    fighters: [
      {
        fighter_id: 700,
        name: "Belal Muhammad",
        corner: "red",
        headshot_url: null,
      },
      {
        fighter_id: 800,
        name: "Shavkat Rakhmonov",
        corner: "blue",
        headshot_url: null,
      },
    ],
  },
];

/**
 * O último evento **realizado** que `GET /events/latest` devolve: o mesmo shape
 * do detalhe (`EventDetailOut`), porque a Home precisa exatamente da mesma
 * forma. Nunca um evento por vir — o card da Home é de um evento que aconteceu.
 */
export const LATEST_EVENT_FIXTURE: EventDetailOut = {
  id: 42,
  name: "UFC 300",
  date: "2024-04-13",
  location: "Las Vegas, USA",
  source: "kaggle",
  bouts: UFC_300_BOUTS,
  summary: UFC_300_SUMMARY,
  highlights: UFC_300_HIGHLIGHTS,
};

/** Detalhe por id do evento; ausente no mapa significa 404. */
export const EVENT_DETAIL_FIXTURES: Record<number, EventDetailOut> = {
  42: LATEST_EVENT_FIXTURE,
  // Evento realizado sem lutas ingeridas: card não publicado.
  40: {
    id: 40,
    name: "UFC Fight Night: Ribas vs. Namajunas",
    date: "2024-03-23",
    location: null,
    source: "kaggle",
    bouts: [],
    summary: null,
    highlights: null,
  },
  // Caso da DT-1: data passada, card inteiro agendado, nenhuma contagem possível.
  801: {
    id: 801,
    name: "UFC 332",
    date: AWAITING_SYNC_EVENT_DATE,
    location: "Las Vegas, USA",
    source: "cito",
    bouts: UFC_332_BOUTS,
    summary: null,
    highlights: null,
  },
  // `UFC 335`: no acervo sem nenhuma luta, com data futura.
  813: {
    id: 813,
    name: "UFC 335",
    date: isoDateFromToday(58),
    location: null,
    source: "cito",
    bouts: [],
    summary: null,
    highlights: null,
  },
};
