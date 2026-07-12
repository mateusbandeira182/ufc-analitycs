import { setupServer } from "msw/node";

import { handlers } from "@/mocks/handlers";

// Servidor MSW compartilhado pelos testes (Node). O ciclo de vida
// (listen/resetHandlers/close) é registrado em src/test/setup.ts.
export const server = setupServer(...handlers);
