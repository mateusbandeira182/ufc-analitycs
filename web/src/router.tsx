import { createBrowserRouter } from "react-router";

import { AppLayout } from "@/components/layout/AppLayout";
import { EventPage } from "@/features/events/EventPage";
import { EventsPage } from "@/features/events/EventsPage";
import { FighterPage } from "@/features/fighters/FighterPage";
import { FightersPage } from "@/features/fighters/FightersPage";
import { HomePage } from "@/routes/HomePage";

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { path: "/", element: <HomePage /> },
      { path: "/fighters", element: <FightersPage /> },
      { path: "/fighters/:id", element: <FighterPage /> },
      { path: "/events", element: <EventsPage /> },
      { path: "/events/:id", element: <EventPage /> },
    ],
  },
]);
