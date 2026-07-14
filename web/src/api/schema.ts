import type { components } from "@/api/types";

/*
  Aliases de conveniência sobre os tipos GERADOS do OpenAPI do backend.
  O shape nunca é redigitado à mão — o contrato do FastAPI é a fonte de verdade.
  Regenerar via `npm run gen:api` quando o backend mudar.
*/

export type FighterOut = components["schemas"]["FighterOut"];
export type PageFighterOut = components["schemas"]["Page_FighterOut_"];
export type Stance = components["schemas"]["Stance"];
export type FighterBoutOut = components["schemas"]["FighterBoutOut"];
export type FighterStatsOut = components["schemas"]["FighterStatsOut"];
export type StrikingProfileOut = components["schemas"]["StrikingProfileOut"];
export type FighterOpponentOut = components["schemas"]["FighterOpponentOut"];
export type BoutMethod = components["schemas"]["BoutMethod"];
export type BoutFighterStatsOut = components["schemas"]["BoutFighterStatsOut"];
export type Corner = components["schemas"]["Corner"];
export type EventOut = components["schemas"]["EventOut"];
export type PageEventOut = components["schemas"]["Page_EventOut_"];
export type EventDetailOut = components["schemas"]["EventDetailOut"];
export type BoutCardOut = components["schemas"]["BoutCardOut"];
export type BoutCardFighterOut = components["schemas"]["BoutCardFighterOut"];
export type BoutDetailOut = components["schemas"]["BoutDetailOut"];
export type HeadToHeadOut = components["schemas"]["HeadToHeadOut"];
export type MatchupPredictionOut =
  components["schemas"]["MatchupPredictionOut"];
export type MatchupFighterOut = components["schemas"]["MatchupFighterOut"];
