import type { FighterOut, FighterStatsOut } from "@/api/schema";

/*
  Fixtures de lutadores para os testes e handlers MSW.
  Espelham o shape real de FighterOut (contrato do M2), incluindo campos anuláveis.
*/
export const FIGHTER_FIXTURES: FighterOut[] = [
  {
    id: 1,
    name: "Jon Jones",
    nickname: "Bones",
    date_of_birth: "1987-07-19",
    height_cm: 193,
    reach_cm: 215,
    weight_kg: 93,
    stance: "orthodox",
    wins: 27,
    losses: 1,
    draws: 0,
    source: "kaggle",
  },
  {
    id: 2,
    name: "Alexander Volkanovski",
    nickname: "The Great",
    date_of_birth: "1988-09-29",
    height_cm: 168,
    reach_cm: 182,
    weight_kg: 66,
    stance: "orthodox",
    wins: 26,
    losses: 4,
    draws: 0,
    source: "kaggle",
  },
  {
    id: 3,
    name: "Israel Adesanya",
    nickname: "The Last Stylebender",
    date_of_birth: "1989-07-22",
    height_cm: 193,
    reach_cm: 203,
    weight_kg: null,
    stance: "switch",
    wins: 24,
    losses: 4,
    draws: 0,
    source: "kaggle",
  },
];

/*
  Estatísticas resumidas por lutador (GET /fighters/:id/stats), computadas on
  demand pelo backend. Espelham FighterStatsOut, incluindo o `striking_profile`
  com os shares por alvo/posição (fração ou nulo quando não há dado).
*/
export const FIGHTER_STATS_FIXTURES: Record<number, FighterStatsOut> = {
  // Jon Jones (id 1): perfil de striking completo para as asserções da tela.
  1: {
    fighter_id: 1,
    bouts_counted: 2,
    avg_sig_strikes_landed: 60,
    avg_takedowns_landed: 1,
    avg_control_time_seconds: 204,
    wins_by_method: { ko_tko: 1 },
    striking_profile: {
      share_head: 0.55,
      share_body: 0.2,
      share_leg: 0.25,
      share_distance: 0.7,
      share_clinch: 0.2,
      share_ground: 0.1,
    },
  },
  // Israel Adesanya (id 3): perfil sem dado (denominador zero -> null no backend).
  3: {
    fighter_id: 3,
    bouts_counted: 0,
    avg_sig_strikes_landed: null,
    avg_takedowns_landed: null,
    avg_control_time_seconds: null,
    wins_by_method: {},
    striking_profile: {
      share_head: null,
      share_body: null,
      share_leg: null,
      share_distance: null,
      share_clinch: null,
      share_ground: null,
    },
  },
};
