import type { BoutFighterStatsOut } from "@/api/schema";

/*
  Padrões nulos dos splits granulares (M5) do box-score por canto. O contrato os
  exige em BoutFighterStatsOut, mas o histórico/detalhe da luta não os exibe nesta
  fatia — as fixtures os preenchem com `null` (sem dado) via este spread único,
  evitando repetir os 15 campos em cada factory.
*/
export const NULL_STRIKE_SPLITS: Pick<
  BoutFighterStatsOut,
  | "total_strikes_landed"
  | "total_strikes_attempted"
  | "head_landed"
  | "head_attempted"
  | "body_landed"
  | "body_attempted"
  | "leg_landed"
  | "leg_attempted"
  | "distance_landed"
  | "distance_attempted"
  | "clinch_landed"
  | "clinch_attempted"
  | "ground_landed"
  | "ground_attempted"
  | "reversals"
> = {
  total_strikes_landed: null,
  total_strikes_attempted: null,
  head_landed: null,
  head_attempted: null,
  body_landed: null,
  body_attempted: null,
  leg_landed: null,
  leg_attempted: null,
  distance_landed: null,
  distance_attempted: null,
  clinch_landed: null,
  clinch_attempted: null,
  ground_landed: null,
  ground_attempted: null,
  reversals: null,
};
