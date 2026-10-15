import type { components, operations } from "@/api/types";

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
export type BoutStatus = components["schemas"]["BoutStatus"];
export type EventSummaryOut = components["schemas"]["EventSummaryOut"];
export type EventHighlightsOut = components["schemas"]["EventHighlightsOut"];
export type FinishHighlightOut = components["schemas"]["FinishHighlightOut"];
export type BoutHighlightOut = components["schemas"]["BoutHighlightOut"];
export type UpsetHighlightOut = components["schemas"]["UpsetHighlightOut"];

/**
 * Partição da lista de eventos. O backend a declara como `Literal` no query
 * param, não como schema nomeado, então o tipo é derivado da **operação** do
 * contrato — redigitar `"past" | "upcoming"` à mão deixaria o cliente aceitando
 * um valor que o FastAPI rejeita com 422 no dia em que a partição mudar.
 */
export type EventWhen = NonNullable<
  NonNullable<
    operations["list_events_endpoint_api_v1_events_get"]["parameters"]["query"]
  >["when"]
>;
export type BoutDetailOut = components["schemas"]["BoutDetailOut"];
export type HeadToHeadOut = components["schemas"]["HeadToHeadOut"];
export type MatchupPredictionOut =
  components["schemas"]["MatchupPredictionOut"];
export type EventPredictionOut = components["schemas"]["EventPredictionOut"];
export type CardBoutPredictionOut =
  components["schemas"]["CardBoutPredictionOut"];
export type MatchupFighterOut = components["schemas"]["MatchupFighterOut"];
export type FeaturesFreshnessOut =
  components["schemas"]["FeaturesFreshnessOut"];
export type UnavailableCode = components["schemas"]["UnavailableCode"];
export type CurrentFormOut = components["schemas"]["CurrentFormOut"];
export type MatchupBlockInfluenceOut =
  components["schemas"]["MatchupBlockInfluenceOut"];
export type MatchupAttributeOut = components["schemas"]["MatchupAttributeOut"];
export type CommonOpponentOut = components["schemas"]["CommonOpponentOut"];
export type CommonOpponentBoutOut =
  components["schemas"]["CommonOpponentBoutOut"];

/**
 * Cobertura do bloco no serving. O contrato declara o enum **inline** no campo,
 * não como schema nomeado, então o tipo é derivado do próprio campo — redigitar
 * `"full" | "partial" | "none"` deixaria o cliente aceitando um valor que o
 * backend não emite no dia em que a escala mudar, o mesmo defeito que `EventWhen`
 * resolve por derivação.
 */
export type BlockCoverage = MatchupBlockInfluenceOut["coverage"];
