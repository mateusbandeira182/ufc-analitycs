import type {
  CurrentFormOut,
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
    body_image_url: "https://imagens.ufc.test/jon-jones-corpo.png",
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
  Sprint 01). O padrão é em dia; as duas variantes abaixo cobrem os dois estados
  que o aviso de defasagem distingue — cache atrás do granular e cache nunca
  materializado. `materialized_at` é um **datetime** (emenda DP-05 da Sprint 01),
  e é essa a forma que chega à tela.
*/
export const FRESH_FEATURES: FeaturesFreshnessOut = {
  materialized_at: "2026-09-01T00:00:00Z",
  n_bouts_source: 2,
  n_bouts_current: 2,
  stale: false,
};

/** Cache materializado, mas atrás do granular: o aviso aparece com data e comando. */
export const STALE_FEATURES: FeaturesFreshnessOut = {
  materialized_at: "2026-09-01T00:00:00Z",
  n_bouts_source: 2,
  n_bouts_current: 5,
  stale: true,
};

/*
  Cache nunca materializado: `materialized_at` nulo, nenhuma luta coberta e
  defasado por definição. É o estado em que `current_form` vem nulo sem que o
  lutador seja estreante — quem distingue os dois motivos é este bloco.
*/
export const NEVER_MATERIALIZED_FEATURES: FeaturesFreshnessOut = {
  materialized_at: null,
  n_bouts_source: 0,
  n_bouts_current: 5,
  stale: true,
};

/*
  Forma atual as-of de Jon Jones (RF-15): os treze campos lidos de
  `fighter_asof_features`, a mesma tabela que alimenta a predição. Os nomes são
  os das features verbatim (DP-08) — a tela os rotula em pt-BR.

  A fixture é deliberadamente irregular: uma tendência negativa
  (`sig_strikes_absorbed_pm_trend`, que negativo é MELHOR — absorveu menos), uma
  tendência exatamente zero (janela igual à carreira, medição legítima) e um
  campo nulo (`control_time_avg_trend`), porque ausência dentro do payload tem de
  chegar à tela como ausência, nunca como zero.
*/
export const JON_JONES_CURRENT_FORM: CurrentFormOut = {
  sig_strikes_landed_pm_r3: 4.2,
  sig_strikes_absorbed_pm_r3: 2.8,
  takedowns_landed_avg_r3: 1.5,
  takedown_defense_r3: 0.64,
  control_time_avg_r3: 186,
  sig_strikes_landed_pm_trend: 0.7,
  sig_strikes_absorbed_pm_trend: -0.3,
  takedowns_landed_avg_trend: 0,
  takedown_defense_trend: 0.05,
  control_time_avg_trend: null,
  career_minutes_before: 312.5,
  ko_losses_prior: 0,
  submission_losses_prior: 1,
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
    current_form: JON_JONES_CURRENT_FORM,
    features_freshness: FRESH_FEATURES,
  },
  // Israel Adesanya (id 3): perfil sem dado (denominador zero -> null no backend)
  // e **sem linha** no cache as-of — lutador sem histórico suficiente. O bloco
  // inteiro vem nulo, nunca um vetor de zeros.
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
    current_form: null,
    features_freshness: FRESH_FEATURES,
  },
};
