import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { EventPage } from "@/features/events/EventPage";
import { EventsPage } from "@/features/events/EventsPage";
import { EVENT_DETAIL_FIXTURES } from "@/mocks/events";
import { server } from "@/mocks/server";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderEventPage(id: number | string) {
  return renderWithProviders(<EventPage />, {
    routes: [{ path: "/events/:id", element: <EventPage /> }],
    initialEntries: [`/events/${String(id)}`],
  });
}

describe("EventPage", () => {
  it("mostra o skeleton enquanto o evento carrega", () => {
    server.use(
      http.get("*/api/v1/events/:id", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    renderEventPage(42);

    expect(screen.getByLabelText(/carregando evento/i)).toBeInTheDocument();
  });

  it("exibe o cabeçalho com nome, data formatada e local no sucesso", async () => {
    renderEventPage(42);

    expect(
      await screen.findByRole("heading", { name: /ufc 300/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("13/04/2024")).toBeInTheDocument();
    expect(screen.getByText(/las vegas, usa/i)).toBeInTheDocument();
  });

  it("renderiza o card 'A vs B' com cada lutador linkando para /fighters/:id", async () => {
    renderEventPage(42);

    const pereira = await screen.findByRole("link", {
      name: /alex pereira/i,
    });
    expect(pereira).toHaveAttribute("href", "/fighters/100");

    const hill = screen.getByRole("link", { name: /jamahal hill/i });
    expect(hill).toHaveAttribute("href", "/fighters/200");
  });

  it("destaca o vencedor cruzando winner_id com fighter_id", async () => {
    renderEventPage(42);

    const winner = await screen.findByRole("link", {
      name: /alex pereira, vencedor/i,
    });
    expect(winner).toHaveAttribute("href", "/fighters/100");

    // O perdedor não recebe a marca de vencedor.
    expect(
      screen.getByRole("link", { name: /jamahal hill/i }),
    ).not.toHaveAccessibleName(/vencedor/i);
  });

  it("não destaca nenhum canto quando winner_id é nulo (no contest)", async () => {
    renderEventPage(42);

    // O UFC 300 tem uma segunda luta sem vencedor (winner_id nulo).
    const oliveira = await screen.findByRole("link", {
      name: /charles oliveira/i,
    });
    expect(oliveira).not.toHaveAccessibleName(/vencedor/i);
    expect(
      screen.getByRole("link", { name: /arman tsarukyan/i }),
    ).not.toHaveAccessibleName(/vencedor/i);
  });

  it("exibe o resultado formatado (método, round/tempo e categoria de peso)", async () => {
    renderEventPage(42);

    const bout = await screen.findByRole("listitem", {
      name: /alex pereira vs jamahal hill/i,
    });
    expect(within(bout).getByText("KO/TKO")).toBeInTheDocument();
    expect(within(bout).getByText("R2 3:45")).toBeInTheDocument();
    expect(within(bout).getByText(/light heavyweight/i)).toBeInTheDocument();
  });

  it("linka cada luta para /bouts/:id", async () => {
    renderEventPage(42);

    const bout = await screen.findByRole("listitem", {
      name: /alex pereira vs jamahal hill/i,
    });
    const boutLink = within(bout).getByRole("link", { name: /ver luta/i });
    expect(boutLink).toHaveAttribute("href", "/bouts/7");
  });

  it("mostra mensagem de card vazio quando o evento não tem lutas", async () => {
    server.use(
      http.get("*/api/v1/events/:id", () =>
        HttpResponse.json(EVENT_DETAIL_FIXTURES[40]),
      ),
    );

    renderEventPage(40);

    expect(
      await screen.findByText(/nenhuma luta registrada/i),
    ).toBeInTheDocument();
  });

  it("mostra 'evento não encontrado' quando o evento não existe (404)", async () => {
    renderEventPage(999);

    expect(
      await screen.findByText(/evento não encontrado/i),
    ).toBeInTheDocument();
  });

  it("mostra 'evento não encontrado' quando o id da rota é inválido", async () => {
    renderEventPage("abc");

    expect(
      await screen.findByText(/evento não encontrado/i),
    ).toBeInTheDocument();
  });

  it("mostra mensagem legível quando o backend falha", async () => {
    server.use(
      http.get("*/api/v1/events/:id", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderEventPage(42);

    expect(
      await screen.findByText(/não foi possível carregar o evento/i),
    ).toBeInTheDocument();
  });

  it("navega da lista para o detalhe do evento ao clicar", async () => {
    const user = userEvent.setup();
    const { router } = renderWithProviders(<EventsPage />, {
      routes: [
        { path: "/events", element: <EventsPage /> },
        { path: "/events/:id", element: <EventPage /> },
      ],
      initialEntries: ["/events"],
    });

    const link = await screen.findByRole("link", { name: /ufc 300/i });
    await user.click(link);

    expect(router.state.location.pathname).toBe("/events/42");
    expect(
      await screen.findByRole("heading", { name: /ufc 300/i }),
    ).toBeInTheDocument();
  });
});
