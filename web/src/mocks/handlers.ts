import { http, HttpResponse } from "msw";

import type {
  HeadToHeadOut,
  MatchupPredictionOut,
  PageEventOut,
  PageFighterOut,
} from "@/api/schema";
import { BOUT_DETAIL_FIXTURES, BOUT_FIXTURES } from "@/mocks/bouts";
import { EVENT_PREDICTION_FIXTURES } from "@/mocks/eventPrediction";
import {
  EVENT_DETAIL_FIXTURES,
  EVENT_FIXTURES,
  LATEST_EVENT_FIXTURE,
  PAST_EVENT_FIXTURES,
  UPCOMING_EVENT_FIXTURES,
} from "@/mocks/events";
import {
  FIGHTER_FIXTURES,
  FIGHTER_STATS_FIXTURES,
  FRESH_FEATURES,
} from "@/mocks/fighters";
import { commonOpponents, headToHeadBouts } from "@/mocks/headToHead";
import {
  MATCHUP_ATTRIBUTE_FIXTURES,
  MATCHUP_BLOCK_FIXTURES,
} from "@/mocks/matchup";

/*
  Handlers MSW espelhando o contrato: GET /api/v1/fighters devolve o envelope
  paginado Page[FighterOut], filtra server-side por `q` (substring,
  case-insensitive, em **nome ou apelido**) e pagina por `limit`/`offset`, como
  o backend faz. `total` reflete o conjunto filtrado inteiro (não só a página).
  O handler não reimplementa o escape de curinga: tratar `%`/`_` como texto
  literal é responsabilidade do Postgres, coberta pelos testes de Selector e de
  API do backend. Os testes podem sobrescrever handlers específicos (erro 500,
  lista vazia) via server.use(...).
*/

/** Lê `limit`/`offset` da query string com os mesmos padrões do backend. */
function readPageParams(url: URL): { limit: number; offset: number } {
  const limit = Number(url.searchParams.get("limit") ?? 50);
  const offset = Number(url.searchParams.get("offset") ?? 0);
  return { limit, offset };
}

export const handlers = [
  http.get("*/api/v1/fighters", ({ request }) => {
    const url = new URL(request.url);
    const q = url.searchParams.get("q")?.trim().toLowerCase();
    const { limit, offset } = readPageParams(url);

    const filtered = q
      ? FIGHTER_FIXTURES.filter(
          (f) =>
            f.name.toLowerCase().includes(q) ||
            (f.nickname?.toLowerCase().includes(q) ?? false),
        )
      : FIGHTER_FIXTURES;

    const body: PageFighterOut = {
      items: filtered.slice(offset, offset + limit),
      total: filtered.length,
      limit,
      offset,
    };
    return HttpResponse.json(body);
  }),

  // Detalhe do lutador: 404 quando o id não existe no acervo.
  http.get("*/api/v1/fighters/:id", ({ params }) => {
    const id = Number(params.id);
    const fighter = FIGHTER_FIXTURES.find((f) => f.id === id);
    if (!fighter) {
      return HttpResponse.json({ detail: "Not Found" }, { status: 404 });
    }
    return HttpResponse.json(fighter);
  }),

  // Histórico de lutas em ordem cronológica (o backend já ordena); vazio por padrão.
  http.get("*/api/v1/fighters/:id/bouts", ({ params }) => {
    const id = Number(params.id);
    return HttpResponse.json(BOUT_FIXTURES[id] ?? []);
  }),

  // Estatísticas resumidas do lutador (médias + perfil de striking); 404 quando
  // o id não existe no acervo.
  http.get("*/api/v1/fighters/:id/stats", ({ params }) => {
    const id = Number(params.id);
    const stats = FIGHTER_STATS_FIXTURES[id];
    if (!stats) {
      return HttpResponse.json({ detail: "Not Found" }, { status: 404 });
    }
    return HttpResponse.json(stats);
  }),

  /*
    Lista de eventos: envelope Page[EventOut] paginado por `limit`/`offset`.
    `when=past|upcoming` particiona o acervo e o `total` reflete o conjunto
    **filtrado**, como no backend — um `total` do acervo inteiro faria a
    paginação de cada seção mentir enquanto os itens pareceriam certos.
  */
  http.get("*/api/v1/events", ({ request }) => {
    const url = new URL(request.url);
    const { limit, offset } = readPageParams(url);
    const when = url.searchParams.get("when");

    const archive =
      when === "past"
        ? PAST_EVENT_FIXTURES
        : when === "upcoming"
          ? UPCOMING_EVENT_FIXTURES
          : EVENT_FIXTURES;

    const body: PageEventOut = {
      items: archive.slice(offset, offset + limit),
      total: archive.length,
      limit,
      offset,
    };
    return HttpResponse.json(body);
  }),

  /*
    Último evento realizado. Declarado **antes** de `/events/:id` de propósito:
    na ordem inversa o `:id` casaria o literal "latest" e o teste passaria
    contra um mock que não reflete o servidor (onde o FastAPI devolveria 422).
  */
  http.get("*/api/v1/events/latest", () =>
    HttpResponse.json(LATEST_EVENT_FIXTURE),
  ),

  // Detalhe do evento com o card de lutas: 404 quando o id não existe no acervo.
  http.get("*/api/v1/events/:id", ({ params }) => {
    const id = Number(params.id);
    const event = EVENT_DETAIL_FIXTURES[id];
    if (!event) {
      return HttpResponse.json({ detail: "Not Found" }, { status: 404 });
    }
    return HttpResponse.json(event);
  }),

  // Detalhe da luta com stats granulares: 404 quando o id não existe no acervo.
  http.get("*/api/v1/bouts/:id", ({ params }) => {
    const id = Number(params.id);
    const bout = BOUT_DETAIL_FIXTURES[id];
    if (!bout) {
      return HttpResponse.json({ detail: "Not Found" }, { status: 404 });
    }
    return HttpResponse.json(bout);
  }),

  /*
    Confronto direto entre dois lutadores, espelhando as regras do M2:
    a == b -> 422 (devem ser distintos); a ou b inexistente -> 404; ambos
    existentes sem confronto -> 200 com bouts vazio (distinto do 404).
  */
  http.get("*/api/v1/head-to-head", ({ request }) => {
    const query = new URL(request.url).searchParams;
    const a = Number(query.get("a"));
    const b = Number(query.get("b"));

    if (a === b) {
      return HttpResponse.json(
        { detail: "Os lutadores devem ser distintos." },
        { status: 422 },
      );
    }

    const hasA = FIGHTER_FIXTURES.some((f) => f.id === a);
    const hasB = FIGHTER_FIXTURES.some((f) => f.id === b);
    if (!hasA || !hasB) {
      return HttpResponse.json({ detail: "Not Found" }, { status: 404 });
    }

    const body: HeadToHeadOut = {
      fighter_a_id: a,
      fighter_b_id: b,
      bouts: headToHeadBouts(a, b),
      // Seção de adversários em comum (RF-16), por par: o (1, 3) tem um, o
      // (1, 2) não tem nenhum porque eles se enfrentaram. Quem precisa de outro
      // cenário sobrescreve o handler com o próprio corpo.
      common_opponents: commonOpponents(a, b),
    };
    return HttpResponse.json(body);
  }),

  /*
    Card previsto do evento. Handler **padrão** porque `setup.ts` roda com
    `onUnhandledRequest: "error"`: sem ele, todo teste de página de evento com
    luta agendada quebraria na requisição não tratada, e não pelo que afirma.

    Atenção ao que este endpoint é no servidor real: um GET que **escreve** em
    `bout_predictions`. Quem prova que a página de evento realizado não o dispara
    é o handler-espião do teste, não este mock.
  */
  http.get("*/api/v1/predict/event/:id", ({ params }) => {
    const id = Number(params.id);
    const prediction = EVENT_PREDICTION_FIXTURES[id];
    if (!prediction) {
      return HttpResponse.json({ detail: "Not Found" }, { status: 404 });
    }
    return HttpResponse.json(prediction);
  }),

  /*
    Palpite neutro de canto do modelo preditivo, espelhando as regras do router:
    fighter_a == fighter_b -> 422; lutador inexistente -> 404. Estreante NÃO é mais
    422 (D-04): responde 200 com probabilidades nulas e `unavailable_code`, e quem
    exercita esse caminho sobrescreve o handler. No caminho feliz a
    probabilidade é derivada de forma determinística das vitórias das fixtures
    (proporção de cartel), para as asserções dos testes; as duas somam 1 e o
    vencedor previsto é o de maior probabilidade.
  */
  http.get("*/api/v1/predict/matchup", ({ request }) => {
    const query = new URL(request.url).searchParams;
    const a = Number(query.get("fighter_a"));
    const b = Number(query.get("fighter_b"));

    if (a === b) {
      return HttpResponse.json(
        { detail: "fighter_a e fighter_b devem ser lutadores distintos" },
        { status: 422 },
      );
    }

    const fighterA = FIGHTER_FIXTURES.find((f) => f.id === a);
    const fighterB = FIGHTER_FIXTURES.find((f) => f.id === b);
    if (!fighterA || !fighterB) {
      return HttpResponse.json({ detail: "Not Found" }, { status: 404 });
    }

    const probA = fighterA.wins / (fighterA.wins + fighterB.wins);
    const body: MatchupPredictionOut = {
      fighter_a: { id: fighterA.id, name: fighterA.name },
      fighter_b: { id: fighterB.id, name: fighterB.name },
      prob_a_wins: probA,
      prob_b_wins: 1 - probA,
      predicted_winner_id: probA >= 0.5 ? fighterA.id : fighterB.id,
      // Caminho feliz: nada indisponível, e a explicabilidade preenchida — os
      // onze blocos de ablação com as coberturas medidas na Sprint 05 e os oito
      // atributos as-of crus dos dois lados. Quem exercita o estado
      // indisponível sobrescreve o handler com `unavailableMatchup`.
      unavailable_reason: null,
      unavailable_code: null,
      blocks: MATCHUP_BLOCK_FIXTURES,
      attributes: MATCHUP_ATTRIBUTE_FIXTURES,
      features_freshness: FRESH_FEATURES,
    };
    return HttpResponse.json(body);
  }),
];
