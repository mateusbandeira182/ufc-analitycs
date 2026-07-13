import { createBrowserRouter } from "react-router";

import { AppLayout } from "@/components/layout/AppLayout";
import { BoutDetail } from "@/features/bouts/BoutDetail";
import { EventPage } from "@/features/events/EventPage";
import { EventsPage } from "@/features/events/EventsPage";
import { FighterPage } from "@/features/fighters/FighterPage";
import { FighterStatsPage } from "@/features/fighters/FighterStatsPage";
import { FightersPage } from "@/features/fighters/FightersPage";
import { HeadToHeadPage } from "@/features/head-to-head/HeadToHeadPage";
import { HomePage } from "@/routes/HomePage";

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { path: "/", element: <HomePage /> },
      { path: "/fighters", element: <FightersPage /> },
      { path: "/fighters/:id", element: <FighterPage /> },
      { path: "/fighters/:id/stats", element: <FighterStatsPage /> },
      { path: "/events", element: <EventsPage /> },
      { path: "/events/:id", element: <EventPage /> },
      { path: "/bouts/:id", element: <BoutDetail /> },
      { path: "/head-to-head", element: <HeadToHeadPage /> },
    ],
  },
]);
