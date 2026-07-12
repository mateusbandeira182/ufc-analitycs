import type { BoutCardOut, EventDetailOut, EventOut } from "@/api/schema";

/*
  Fixtures de eventos para os testes e handlers MSW. Espelham o shape real de
  EventOut / EventDetailOut (contrato do M2). A lista vem em ordem decrescente de
  data (mais recentes primeiro), como o backend entrega — o cliente não reordena.
*/
export const EVENT_FIXTURES: EventOut[] = [
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
  Card do UFC 300 (id 42): duas lutas. A primeira tem vencedor definido (canto
  vermelho) e categoria de peso; a segunda é um no contest com `winner_id` nulo
  (ninguém destacado) e sem categoria — para cobrir os anuláveis do contrato.
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
    fighters: [
      { fighter_id: 100, name: "Alex Pereira", corner: "red" },
      { fighter_id: 200, name: "Jamahal Hill", corner: "blue" },
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
    fighters: [
      { fighter_id: 300, name: "Charles Oliveira", corner: "red" },
      { fighter_id: 400, name: "Arman Tsarukyan", corner: "blue" },
    ],
  },
];

/** Detalhe por id do evento; ausente no mapa significa 404. */
export const EVENT_DETAIL_FIXTURES: Record<number, EventDetailOut> = {
  42: {
    id: 42,
    name: "UFC 300",
    date: "2024-04-13",
    location: "Las Vegas, USA",
    source: "kaggle",
    bouts: UFC_300_BOUTS,
  },
  // Evento sem lutas cadastradas: card vazio.
  40: {
    id: 40,
    name: "UFC Fight Night: Ribas vs. Namajunas",
    date: "2024-03-23",
    location: null,
    source: "kaggle",
    bouts: [],
  },
};
