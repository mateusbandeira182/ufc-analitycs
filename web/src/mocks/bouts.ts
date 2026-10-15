import type {
  BoutDetailOut,
  BoutFighterStatsOut,
  FighterBoutOut,
} from "@/api/schema";
import { AWAITING_SYNC_EVENT_DATE } from "@/mocks/events";
import { NULL_STRIKE_SPLITS } from "@/mocks/strikeSplits";

/*
  Fixtures do histórico de lutas para os testes e handlers MSW.
  Espelham o shape real de FighterBoutOut (contrato do M2). O `stats` granular
  não é exibido nesta slice, mas o contrato o exige — preenchido com um valor
  mínimo. A ordem da lista é a ordem cronológica que o backend garante.
*/

/** Stats mínimas para satisfazer o contrato; o box-score é exibido só na Slice 04. */
function stubStats(fighterId: number, name: string): BoutFighterStatsOut {
  return {
    fighter_id: fighterId,
    name,
    corner: "red",
    knockdowns: null,
    sig_strikes_landed: null,
    sig_strikes_attempted: null,
    takedowns_landed: null,
    takedowns_attempted: null,
    submission_attempts: null,
    control_time_seconds: null,
    ...NULL_STRIKE_SPLITS,
    source: "kaggle",
  };
}

/*
  Stats do canto no histórico com box-score preenchido: o histórico em si não
  exibe o box-score, mas a página de estatísticas recompõe as médias a partir
  dele (recorte por período / últimas N). Só os três números agregados importam.
*/
function historyStats(
  box: Pick<
    BoutFighterStatsOut,
    "sig_strikes_landed" | "takedowns_landed" | "control_time_seconds"
  >,
): BoutFighterStatsOut {
  return { ...stubStats(1, "Jon Jones"), ...box };
}

/*
  Histórico do lutador 1 (Jon Jones) em ordem cronológica: uma derrota por
  decisão mais antiga e uma vitória por nocaute mais recente. Datas distintas
  para travar a ordem no teste; o box-score alimenta as médias da página de stats
  (média de golpes 60,0; quedas 1,0; controle 3:24 sobre as duas lutas).
*/
export const JON_JONES_BOUTS: FighterBoutOut[] = [
  {
    bout_id: 91,
    event_id: 41,
    event_name: "UFC 152",
    event_date: "2012-09-22",
    method: "decision",
    round: 3,
    ending_time_seconds: 300,
    won: false,
    stats: historyStats({
      sig_strikes_landed: 50,
      takedowns_landed: 2,
      control_time_seconds: 120,
    }),
    opponent: { fighter_id: 2, name: "Alexander Volkanovski" },
  },
  {
    bout_id: 92,
    event_id: 42,
    event_name: "UFC 200",
    event_date: "2016-07-09",
    method: "ko_tko",
    round: 2,
    ending_time_seconds: 255,
    won: true,
    stats: historyStats({
      sig_strikes_landed: 70,
      takedowns_landed: 0,
      control_time_seconds: 288,
    }),
    opponent: { fighter_id: 3, name: "Israel Adesanya" },
  },
];

/** Histórico com um adversário nulo (dado sujo) para cobrir o fallback sem link. */
export const BOUTS_WITH_NULL_OPPONENT: FighterBoutOut[] = [
  {
    bout_id: 93,
    event_id: 43,
    event_name: "UFC 210",
    event_date: "2017-04-08",
    method: "no_contest",
    round: null,
    ending_time_seconds: null,
    won: false,
    stats: stubStats(1, "Jon Jones"),
    opponent: null,
  },
];

/** Histórico por lutador; ausente no mapa significa lista vazia. */
export const BOUT_FIXTURES: Record<number, FighterBoutOut[]> = {
  1: JON_JONES_BOUTS,
};

/*
  Stats granulares de um canto para o detalhe da luta (Slice 04). Diferente do
  `stubStats` acima (histórico, box-score não exibido), estas trazem o box-score
  completo — os testes asseguram que cada número aparece na página.
*/
function cornerStats(
  fighterId: number,
  name: string,
  corner: "red" | "blue",
  box: Pick<
    BoutFighterStatsOut,
    | "knockdowns"
    | "sig_strikes_landed"
    | "sig_strikes_attempted"
    | "takedowns_landed"
    | "takedowns_attempted"
    | "submission_attempts"
    | "control_time_seconds"
  >,
): BoutFighterStatsOut {
  return {
    fighter_id: fighterId,
    name,
    corner,
    ...NULL_STRIKE_SPLITS,
    source: "kaggle",
    ...box,
  };
}

/*
  Detalhe do UFC 300 (bout id 7): Pereira (vermelho, vencedor por KO/TKO no 2º
  round) x Hill (azul). Box-score completo nos dois cantos para as asserções.
*/
const PEREIRA_HILL: BoutDetailOut = {
  id: 7,
  event: {
    id: 42,
    name: "UFC 300",
    date: "2024-04-13",
    location: "Las Vegas, USA",
    source: "kaggle",
  },
  winner_id: 100,
  method: "ko_tko",
  round: 2,
  ending_time_seconds: 225,
  weight_class: "Light Heavyweight",
  status: "completed",
  card_section: "Main Card",
  title_bout: true,
  scheduled_rounds: 5,
  referee: "Marc Goddard",
  rounds: [],
  source: "kaggle",
  fighters: [
    cornerStats(100, "Alex Pereira", "red", {
      knockdowns: 1,
      sig_strikes_landed: 45,
      sig_strikes_attempted: 80,
      takedowns_landed: 0,
      takedowns_attempted: 0,
      submission_attempts: 0,
      control_time_seconds: 30,
    }),
    cornerStats(200, "Jamahal Hill", "blue", {
      knockdowns: 0,
      sig_strikes_landed: 22,
      sig_strikes_attempted: 61,
      takedowns_landed: 1,
      takedowns_attempted: 3,
      submission_attempts: 0,
      control_time_seconds: 95,
    }),
  ],
};

/*
  No contest (bout id 8): winner_id nulo — nenhum canto destacado. Um dos cantos
  tem stats nulas (dado incompleto) para cobrir o traço na página.
*/
const OLIVEIRA_TSARUKYAN: BoutDetailOut = {
  id: 8,
  event: {
    id: 42,
    name: "UFC 300",
    date: "2024-04-13",
    location: "Las Vegas, USA",
    source: "kaggle",
  },
  winner_id: null,
  method: "no_contest",
  round: null,
  ending_time_seconds: null,
  weight_class: null,
  status: "completed",
  card_section: null,
  title_bout: false,
  scheduled_rounds: 3,
  referee: null,
  rounds: [],
  source: "kaggle",
  fighters: [
    cornerStats(300, "Charles Oliveira", "red", {
      knockdowns: null,
      sig_strikes_landed: null,
      sig_strikes_attempted: null,
      takedowns_landed: null,
      takedowns_attempted: null,
      submission_attempts: null,
      control_time_seconds: null,
    }),
    cornerStats(400, "Arman Tsarukyan", "blue", {
      knockdowns: 0,
      sig_strikes_landed: 10,
      sig_strikes_attempted: 25,
      takedowns_landed: 2,
      takedowns_attempted: 4,
      submission_attempts: 1,
      control_time_seconds: 140,
    }),
  ],
};

/*
  Empate (bout id 9): winner_id nulo com método de decisão (o empate mora na
  nulabilidade do vencedor, não num método próprio) — nenhum canto destacado.
*/
const HOLLOWAY_POIRIER: BoutDetailOut = {
  id: 9,
  event: {
    id: 41,
    name: "UFC 299",
    date: "2024-03-09",
    location: "Miami, USA",
    source: "kaggle",
  },
  winner_id: null,
  method: "decision",
  round: 5,
  ending_time_seconds: 300,
  weight_class: "Lightweight",
  status: "completed",
  card_section: "Prelims",
  title_bout: false,
  scheduled_rounds: 5,
  referee: "Herb Dean",
  rounds: [],
  source: "kaggle",
  fighters: [
    cornerStats(500, "Max Holloway", "red", {
      knockdowns: 0,
      sig_strikes_landed: 120,
      sig_strikes_attempted: 210,
      takedowns_landed: 0,
      takedowns_attempted: 1,
      submission_attempts: 0,
      control_time_seconds: 12,
    }),
    cornerStats(600, "Dustin Poirier", "blue", {
      knockdowns: 0,
      sig_strikes_landed: 118,
      sig_strikes_attempted: 205,
      takedowns_landed: 1,
      takedowns_attempted: 2,
      submission_attempts: 0,
      control_time_seconds: 45,
    }),
  ],
};

/*
  Canto de luta **agendada**: box-score inteiramente nulo, porque não há luta de
  onde tirar número. A página não renderiza a tabela com esses nulos — zero em
  golpe significativo é afirmação sobre a luta, e traço também é resposta a outra
  pergunta ("decisão sem tempo registrado"), não a esta.
*/
function scheduledCornerStats(
  fighterId: number,
  name: string,
  corner: "red" | "blue",
): BoutFighterStatsOut {
  return {
    fighter_id: fighterId,
    name,
    corner,
    knockdowns: null,
    sig_strikes_landed: null,
    sig_strikes_attempted: null,
    takedowns_landed: null,
    takedowns_attempted: null,
    submission_attempts: null,
    control_time_seconds: null,
    ...NULL_STRIKE_SPLITS,
    source: "cito",
  };
}

/*
  Luta agendada do `UFC 332` (bout id 90, o mesmo do card do evento): data do
  evento já passada e a luta ainda `scheduled`, porque o `gap_sync` é manual.
  Contexto de card **sem seção** — o caso que o `bout_order` como porteiro de
  `fill_bout_context` produz no acervo antigo —, mas com divisão, formato e luta
  de título.
*/
const MUHAMMAD_RAKHMONOV: BoutDetailOut = {
  id: 90,
  event: {
    id: 801,
    name: "UFC 332",
    date: AWAITING_SYNC_EVENT_DATE,
    location: "Las Vegas, USA",
    source: "cito",
  },
  winner_id: null,
  method: null,
  round: null,
  ending_time_seconds: null,
  weight_class: "Welterweight",
  status: "scheduled",
  card_section: null,
  title_bout: true,
  scheduled_rounds: 5,
  referee: null,
  rounds: [],
  source: "cito",
  fighters: [
    scheduledCornerStats(700, "Belal Muhammad", "red"),
    scheduledCornerStats(800, "Shavkat Rakhmonov", "blue"),
  ],
};

/*
  Luta agendada do `UFC 334` (bout id 821), com o contexto de card **completo**:
  o PASSO-00 mediu `card_section` e `bout_order` em 100% das 89 lutas agendadas
  do banco, então contexto preenchido é o caso dominante e o nulo (bout 90) é o
  secundário.
*/
const OLIVEIRA_TSARUKYAN_AGENDADA: BoutDetailOut = {
  id: 821,
  event: {
    id: 812,
    name: "UFC 334",
    date: AWAITING_SYNC_EVENT_DATE,
    location: "Abu Dhabi, UAE",
    source: "cito",
  },
  winner_id: null,
  method: null,
  round: null,
  ending_time_seconds: null,
  weight_class: "Lightweight",
  status: "scheduled",
  card_section: "Main Card",
  title_bout: false,
  scheduled_rounds: 3,
  referee: null,
  rounds: [],
  source: "cito",
  fighters: [
    scheduledCornerStats(300, "Charles Oliveira", "red"),
    scheduledCornerStats(400, "Arman Tsarukyan", "blue"),
  ],
};

/*
  Luta agendada **sem nenhum contexto de card e com um canto só** (bout id 823).
  Os dois estados existem no acervo e aparecem juntos: é a mesma luta mal
  cadastrada que faz o endpoint de previsão responder `missing_corners`.

  Na tela: nenhum bloco de contexto (um quadro vazio rotulado "Contexto do card"
  prometeria dado que não existe) e nenhum link para `/confronto`, que exige dois
  lutadores — link quebrado é pior que a ausência dele.
*/
const AGENDADA_SEM_CONTEXTO: BoutDetailOut = {
  id: 823,
  event: {
    id: 812,
    name: "UFC 334",
    date: AWAITING_SYNC_EVENT_DATE,
    location: "Abu Dhabi, UAE",
    source: "cito",
  },
  winner_id: null,
  method: null,
  round: null,
  ending_time_seconds: null,
  weight_class: null,
  status: "scheduled",
  card_section: null,
  title_bout: null,
  scheduled_rounds: null,
  referee: null,
  rounds: [],
  source: "cito",
  fighters: [scheduledCornerStats(900, "Caio Borralho", "red")],
};

/** Detalhe por id da luta; ausente no mapa significa 404. */
export const BOUT_DETAIL_FIXTURES: Record<number, BoutDetailOut> = {
  7: PEREIRA_HILL,
  8: OLIVEIRA_TSARUKYAN,
  9: HOLLOWAY_POIRIER,
  90: MUHAMMAD_RAKHMONOV,
  821: OLIVEIRA_TSARUKYAN_AGENDADA,
  823: AGENDADA_SEM_CONTEXTO,
};
