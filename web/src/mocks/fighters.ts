import type {
  FeaturesFreshnessOut,
  FighterOut,
  FighterStatsOut,
} from "@/api/schema";

/*
  Fixtures de lutadores para os testes e handlers MSW.
  Espelham o shape real de FighterOut, incluindo campos anuláveis. As imagens
  (`headshot_url`, `body_image_url`) entraram no contrato com o M7 e têm
  cobertura parcial no acervo real — só 794 dos 2.754 lutadores têm retrato, e
  lutador histórico nunca terá. As fixtures reproduzem os dois lados de
  propósito: Jon Jones com retrato, os demais sem.
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
    headshot_url: "https://imagens.ufc.test/jon-jones-headshot.png",
    body_image_url: null,
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
    headshot_url: null,
    body_image_url: null,
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
    headshot_url: null,
    body_image_url: null,
    source: "kaggle",
  },
];

/*
  Diagnóstico de defasagem do cache as-of que acompanha os agregados (contrato da
  Sprint 01). As fixtures o declaram em dia: o aviso de dado defasado é da
  Sprint 06, com a primeira consumidora real.
*/
export const FRESH_FEATURES: FeaturesFreshnessOut = {
  materialized_at: "2026-09-01T00:00:00Z",
  n_bouts_source: 2,
  n_bouts_current: 2,
  stale: false,
};

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
    features_freshness: FRESH_FEATURES,
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
    features_freshness: FRESH_FEATURES,
  },
};
