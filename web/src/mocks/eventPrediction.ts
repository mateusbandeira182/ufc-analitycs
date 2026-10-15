import type {
  CardBoutPredictionOut,
  EventPredictionOut,
  UnavailableCode,
} from "@/api/schema";
import { FRESH_FEATURES, STALE_FEATURES } from "@/mocks/fighters";
import { UNAVAILABLE_REASONS } from "@/mocks/matchup";

/*
  Fixtures do card previsto (`GET /predict/event/{id}`), espelhando
  `EventPredictionOut`: uma entrada por luta do card — inclusive as realizadas,
  porque `predict_event_card` reusa `list_event_bouts`, que não filtra `status`.
  A tela é que só consome a entrada da luta agendada.

  A prosa de indisponibilidade vem de `UNAVAILABLE_REASONS` (`@/mocks/matchup`) e
  a de defasagem de `@/mocks/fighters`: o texto do aviso tem **uma** fonte, senão
  os dois conjuntos divergiriam no primeiro ajuste de redação.
*/

/** `trained_at` do artefato, que é o que o contrato chama de versão do modelo. */
const MODEL_VERSION = "2026-10-01T03:12:44Z";

/** Contagem de features do artefato medida no M8 (re-treino 39 -> 57). */
const N_FEATURES = 57;

interface Canto {
  id: number;
  name: string;
}

/**
 * Luta prevista: as duas probabilidades são complementares e o vencedor previsto
 * é o de maior probabilidade, como o backend garante.
 */
function predicted(
  boutId: number,
  red: Canto,
  blue: Canto,
  probRed: number,
): CardBoutPredictionOut {
  return {
    bout_id: boutId,
    fighter_red: red,
    fighter_blue: blue,
    prob_red_wins: probRed,
    prob_blue_wins: 1 - probRed,
    predicted_winner_id: probRed >= 0.5 ? red.id : blue.id,
    unavailable_reason: null,
    unavailable_code: null,
  };
}

/**
 * Luta que o modelo não previu. As três probabilidades vêm **nulas** — nunca
 * `0.5`, que se leria como opinião do modelo quando é ausência dela (RF-21).
 */
function unavailable(
  boutId: number,
  red: Canto | null,
  blue: Canto | null,
  code: UnavailableCode,
): CardBoutPredictionOut {
  return {
    bout_id: boutId,
    fighter_red: red,
    fighter_blue: blue,
    prob_red_wins: null,
    prob_blue_wins: null,
    predicted_winner_id: null,
    unavailable_reason: UNAVAILABLE_REASONS[code],
    unavailable_code: code,
  };
}

const MUHAMMAD: Canto = { id: 700, name: "Belal Muhammad" };
const RAKHMONOV: Canto = { id: 800, name: "Shavkat Rakhmonov" };
const OLIVEIRA: Canto = { id: 300, name: "Charles Oliveira" };
const TSARUKYAN: Canto = { id: 400, name: "Arman Tsarukyan" };
const BORRALHO: Canto = { id: 900, name: "Caio Borralho" };
const ESTREANTE: Canto = { id: 910, name: "Dakota Ferreira" };
const PEREIRA: Canto = { id: 100, name: "Alex Pereira" };
const HILL: Canto = { id: 200, name: "Jamahal Hill" };

/** Card do `UFC 332` (id 801): uma luta agendada, previsível. */
const UFC_332_PREDICTION: EventPredictionOut = {
  event_id: 801,
  model_version: MODEL_VERSION,
  n_features: N_FEATURES,
  bouts: [predicted(90, MUHAMMAD, RAKHMONOV, 0.58)],
  features_freshness: FRESH_FEATURES,
};

/*
  Card misto do `UFC 334` (id 812): a luta realizada também recebe entrada (o
  endpoint não filtra `status`), a do meio é previsível e a das preliminares tem
  um estreante — o estado que a SPEC existe para declarar.
*/
const UFC_334_PREDICTION: EventPredictionOut = {
  event_id: 812,
  model_version: MODEL_VERSION,
  n_features: N_FEATURES,
  bouts: [
    predicted(820, PEREIRA, HILL, 0.71),
    predicted(821, OLIVEIRA, TSARUKYAN, 0.46),
    unavailable(822, BORRALHO, ESTREANTE, "missing_history"),
  ],
  features_freshness: FRESH_FEATURES,
};

/*
  Card inteiramente realizado do `UFC 300` (id 42). Existe só para o
  handler-espião ter corpo a devolver **se** a requisição partisse: a página de
  evento realizado não a dispara, e é isso que o espião prova (CA-P07).
*/
const UFC_300_PREDICTION: EventPredictionOut = {
  event_id: 42,
  model_version: MODEL_VERSION,
  n_features: N_FEATURES,
  bouts: [
    predicted(7, PEREIRA, HILL, 0.64),
    predicted(8, OLIVEIRA, TSARUKYAN, 0.52),
  ],
  features_freshness: FRESH_FEATURES,
};

/** Card previsto por id de evento; ausente no mapa significa 404. */
export const EVENT_PREDICTION_FIXTURES: Record<number, EventPredictionOut> = {
  42: UFC_300_PREDICTION,
  801: UFC_332_PREDICTION,
  812: UFC_334_PREDICTION,
};

/**
 * Card do `UFC 334` com a luta das preliminares indisponível pelo `code` dado.
 * Os **três** códigos são alcançáveis no card, diferente do matchup isolado:
 * `missing_corners` e `repeated_fighter` são estados do próprio card.
 *
 * `missing_corners` zera os dois cantos do payload de previsão — é literalmente
 * a luta sem um lutador no vermelho e um no azul.
 */
export function cardPredictionWithCode(
  code: UnavailableCode,
): EventPredictionOut {
  const cantos: [Canto | null, Canto | null] =
    code === "missing_corners" ? [null, null] : [BORRALHO, ESTREANTE];

  return {
    ...UFC_334_PREDICTION,
    bouts: [
      predicted(820, PEREIRA, HILL, 0.71),
      predicted(821, OLIVEIRA, TSARUKYAN, 0.46),
      unavailable(822, cantos[0], cantos[1], code),
    ],
  };
}

/**
 * Card do `UFC 334` com o cache as-of **defasado**. O palpite continua servido;
 * o que muda é a tela declarar de onde ele veio (RF-06 / D-10).
 */
export const STALE_CARD_PREDICTION: EventPredictionOut = {
  ...UFC_334_PREDICTION,
  features_freshness: STALE_FEATURES,
};

/**
 * Card do `UFC 334` com a prosa de indisponibilidade **trocada** por um texto
 * arbitrário, mantendo o `unavailable_code`. Fixa a DT-6: se algum caminho da
 * tela derivasse a causa parseando `unavailable_reason`, o aviso mudaria.
 */
export const CARD_PREDICTION_WITH_FOREIGN_PROSE: EventPredictionOut = {
  ...UFC_334_PREDICTION,
  bouts: [
    predicted(820, PEREIRA, HILL, 0.71),
    predicted(821, OLIVEIRA, TSARUKYAN, 0.46),
    {
      ...unavailable(822, BORRALHO, ESTREANTE, "missing_history"),
      unavailable_reason:
        "Prosa completamente diferente, vinda de outro lugar.",
    },
  ],
};
