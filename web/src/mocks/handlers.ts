import { http, HttpResponse } from "msw";

import type { PageFighterOut } from "@/api/schema";
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
];
