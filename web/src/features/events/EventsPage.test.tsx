import { screen, within } from "@testing-library/react";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { EventsPage } from "@/features/events/EventsPage";
import { server } from "@/mocks/server";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderEventsPage() {
  return renderWithProviders(<EventsPage />, {
    routes: [{ path: "/events", element: <EventsPage /> }],
    initialEntries: ["/events"],
  });
}

describe("EventsPage", () => {
  it("mostra o skeleton de carregamento enquanto os dados chegam", () => {
    server.use(
      http.get("*/api/v1/events", async () => {
        await delay("infinite");
        return HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 });
      }),
    );

    renderEventsPage();

    expect(screen.getByTestId("events-loading")).toBeInTheDocument();
  });

  it("renderiza os eventos com nome, data formatada e local, mais recentes primeiro", async () => {
    renderEventsPage();

    const list = await screen.findByRole("list", { name: /eventos/i });
    const rows = within(list).getAllByRole("listitem");

    // A ordem é a que o backend entrega — o cliente não reordena.
    expect(rows[0]).toHaveTextContent("UFC 300");
    expect(rows[0]).toHaveTextContent("13/04/2024");
    expect(rows[0]).toHaveTextContent("Las Vegas, USA");
    expect(rows[1]).toHaveTextContent("UFC 299");
  });

  it("renderiza o evento sem local sem quebrar a exibição", async () => {
    renderEventsPage();

    expect(
      await screen.findByText(/ribas vs\. namajunas/i),
    ).toBeInTheDocument();
  });

  it("linka cada evento para /events/:id", async () => {
    renderEventsPage();

    const link = await screen.findByRole("link", { name: /ufc 300/i });
    expect(link).toHaveAttribute("href", "/events/42");
  });

  it("mostra estado vazio quando não há eventos", async () => {
    server.use(
      http.get("*/api/v1/events", () =>
        HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 }),
      ),
    );

    renderEventsPage();

    expect(await screen.findByText(/nenhum evento/i)).toBeInTheDocument();
  });

  it("mostra mensagem legível de erro quando o backend falha", async () => {
    server.use(
      http.get("*/api/v1/events", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderEventsPage();

    expect(
      await screen.findByText(/não foi possível carregar os eventos/i),
    ).toBeInTheDocument();
  });
});
