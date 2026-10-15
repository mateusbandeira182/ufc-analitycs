import {
  createBrowserRouter,
  redirect,
  type LoaderFunction,
} from "react-router";

import { AppLayout } from "@/components/layout/AppLayout";
import { BoutDetail } from "@/features/bouts/BoutDetail";
import { ConfrontoPage } from "@/features/confronto/ConfrontoPage";
import { EventPage } from "@/features/events/EventPage";
import { EventsPage } from "@/features/events/EventsPage";
import { FighterPage } from "@/features/fighters/FighterPage";
import { FightersPage } from "@/features/fighters/FightersPage";
import { HomePage } from "@/routes/HomePage";

/**
 * A página de estatísticas do atleta foi absorvida pela do lutador (SPEC 011,
 * RF-24): a rota antiga sobrevive só como redirecionamento, para não quebrar link
 * já salvo. É `loader` e não um `<Navigate>` porque resolve **antes** de
 * renderizar — sem piscar uma tela intermediária — e não introduz componente
 * nenhum. Exportado para que o teste de rota exercite o mesmo redirecionamento
 * que o app usa, em vez de uma cópia dele.
 */
export const fighterStatsRedirect: LoaderFunction = ({ params }) =>
  redirect(`/fighters/${params.id ?? ""}`);

/**
 * `/head-to-head` e `/matchup` foram absorvidas por `/confronto` (SPEC 011,
 * RF-23). As duas sobrevivem só como redirecionamento, **preservando os
 * parâmetros de query** — link já salvo e link vindo do card de um evento
 * continuam funcionando. Mesmo padrão de `fighterStatsRedirect`: `loader` (e não
 * `<Navigate>`) resolve antes de renderizar, sem piscar tela intermediária.
 * Exportado para que o teste exercite o mesmo redirecionamento que o app usa.
 */
export const confrontoRedirect: LoaderFunction = ({ request }) =>
  redirect(`/confronto${new URL(request.url).search}`);

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { path: "/", element: <HomePage /> },
      { path: "/fighters", element: <FightersPage /> },
      { path: "/fighters/:id", element: <FighterPage /> },
      { path: "/fighters/:id/stats", loader: fighterStatsRedirect },
      { path: "/events", element: <EventsPage /> },
      { path: "/events/:id", element: <EventPage /> },
      { path: "/bouts/:id", element: <BoutDetail /> },
      { path: "/confronto", element: <ConfrontoPage /> },
      { path: "/head-to-head", loader: confrontoRedirect },
      { path: "/matchup", loader: confrontoRedirect },
    ],
  },
]);
