import { http, HttpResponse } from "msw";

import type { PageEventOut, PageFighterOut } from "@/api/schema";
import { BOUT_FIXTURES } from "@/mocks/bouts";
import { EVENT_DETAIL_FIXTURES, EVENT_FIXTURES } from "@/mocks/events";
import { FIGHTER_FIXTURES } from "@/mocks/fighters";

/*
  Handlers MSW espelhando o contrato do M2: GET /api/v1/fighters devolve o
  envelope paginado Page[FighterOut] e filtra server-side por `name` (substring,
  case-insensitive), como o backend faz. Os testes podem sobrescrever handlers
  específicos (erro 500, lista vazia) via server.use(...).
*/
export const handlers = [
  http.get("*/api/v1/fighters", ({ request }) => {
    const name = new URL(request.url).searchParams
      .get("name")
      ?.trim()
      .toLowerCase();

    const items = name
      ? FIGHTER_FIXTURES.filter((f) => f.name.toLowerCase().includes(name))
      : FIGHTER_FIXTURES;

    const body: PageFighterOut = {
      items,
      total: items.length,
      limit: 50,
      offset: 0,
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

  // Lista de eventos: envelope Page[EventOut], mais recentes primeiro (o backend ordena).
  http.get("*/api/v1/events", () => {
    const body: PageEventOut = {
      items: EVENT_FIXTURES,
      total: EVENT_FIXTURES.length,
      limit: 50,
      offset: 0,
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
];
