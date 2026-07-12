import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import {
  createMemoryRouter,
  RouterProvider,
  type RouteObject,
} from "react-router";

type MemoryRouter = ReturnType<typeof createMemoryRouter>;

export interface RenderWithProvidersResult extends RenderResult {
  /** Roteador em memória, para asserir a URL ativa (ex.: `router.state.location`). */
  router: MemoryRouter;
}

interface RenderWithProvidersOptions {
  /** Rotas a montar. Quando omitido, o `ui` é renderizado na rota "/". */
  routes?: RouteObject[];
  /** Entrada inicial do histórico (ex.: "/fighters?name=jon"). */
  initialEntries?: string[];
}

/**
 * Renderiza no ambiente real da SPA: QueryClientProvider (client novo por teste,
 * `retry: false` para o erro aparecer sem espera) + roteador em memória.
 * Reutilizável por todas as slices de frontend.
 */
export function renderWithProviders(
  ui: ReactElement,
  options: RenderWithProvidersOptions = {},
): RenderWithProvidersResult {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  const routes: RouteObject[] = options.routes ?? [{ path: "/", element: ui }];
  const router = createMemoryRouter(routes, {
    initialEntries: options.initialEntries ?? ["/"],
  });

  const result = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );

  return { ...result, router };
}
