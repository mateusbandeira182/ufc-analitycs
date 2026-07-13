import { http, HttpResponse } from "msw";

import type { HeadToHeadOut, PageEventOut, PageFighterOut } from "@/api/schema";
import { BOUT_DETAIL_FIXTURES, BOUT_FIXTURES } from "@/mocks/bouts";
import { EVENT_DETAIL_FIXTURES, EVENT_FIXTURES } from "@/mocks/events";
import { FIGHTER_FIXTURES, FIGHTER_STATS_FIXTURES } from "@/mocks/fighters";
import { headToHeadBouts } from "@/mocks/headToHead";

/*
  Handlers MSW espelhando o contrato do M2: GET /api/v1/fighters devolve o
  envelope paginado Page[FighterOut], filtra server-side por `name` (substring,
  case-insensitive) e pagina por `limit`/`offset`, como o backend faz. `total`
  reflete o conjunto filtrado inteiro (não só a página). Os testes podem
  sobrescrever handlers específicos (erro 500, lista vazia) via server.use(...).
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
    const name = url.searchParams.get("name")?.trim().toLowerCase();
    const { limit, offset } = readPageParams(url);

    const filtered = name
      ? FIGHTER_FIXTURES.filter((f) => f.name.toLowerCase().includes(name))
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

  // Lista de eventos: envelope Page[EventOut], mais recentes primeiro (o backend
  // ordena) e paginado por `limit`/`offset`.
  http.get("*/api/v1/events", ({ request }) => {
    const { limit, offset } = readPageParams(new URL(request.url));
    const body: PageEventOut = {
      items: EVENT_FIXTURES.slice(offset, offset + limit),
      total: EVENT_FIXTURES.length,
      limit,
      offset,
    };
    return HttpResponse.json(body);
  }),

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
    };
    return HttpResponse.json(body);
  }),
];
