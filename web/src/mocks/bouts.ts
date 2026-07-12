import type { BoutFighterStatsOut, FighterBoutOut } from "@/api/schema";

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
    source: "kaggle",
  };
}

/*
  Histórico do lutador 1 (Jon Jones) em ordem cronológica: uma derrota por
  decisão mais antiga e uma vitória por nocaute mais recente. Datas distintas
  para travar a ordem no teste.
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
    stats: stubStats(1, "Jon Jones"),
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
    stats: stubStats(1, "Jon Jones"),
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
