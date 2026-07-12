import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { BoutDetail } from "@/features/bouts/BoutDetail";
import { EventPage } from "@/features/events/EventPage";
import { server } from "@/mocks/server";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderBoutPage(id: number | string) {
  return renderWithProviders(<BoutDetail />, {
    routes: [{ path: "/bouts/:id", element: <BoutDetail /> }],
    initialEntries: [`/bouts/${String(id)}`],
  });
}

describe("BoutDetail", () => {
  it("mostra o skeleton enquanto a luta carrega", () => {
    server.use(
      http.get("*/api/v1/bouts/:id", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    renderBoutPage(7);

    expect(screen.getByLabelText(/carregando luta/i)).toBeInTheDocument();
  });

  it("exibe o cabeçalho com o evento (link), método, round e tempo mm:ss", async () => {
    renderBoutPage(7);

    const eventLink = await screen.findByRole("link", { name: /ufc 300/i });
    expect(eventLink).toHaveAttribute("href", "/events/42");

    const result = screen.getByRole("group", { name: /resultado da luta/i });
    expect(within(result).getByText("KO/TKO")).toBeInTheDocument();
    expect(within(result).getByText(/round 2/i)).toBeInTheDocument();
    expect(within(result).getByText("3:45")).toBeInTheDocument();
    expect(within(result).getByText(/light heavyweight/i)).toBeInTheDocument();
    expect(within(result).getByText(/alex pereira/i)).toBeInTheDocument();
  });

  it("mostra as stats dos dois cantos lado a lado, formatadas", async () => {
    renderBoutPage(7);

    const stats = await screen.findByRole("table", {
      name: /estatísticas por lutador/i,
    });

    // Golpes significativos como "acertados de tentados" nos dois cantos.
    expect(within(stats).getByText("45 de 80")).toBeInTheDocument();
    expect(within(stats).getByText("22 de 61")).toBeInTheDocument();

    // Tempo de controle formatado mm:ss (30s -> 0:30; 95s -> 1:35).
    expect(within(stats).getByText("0:30")).toBeInTheDocument();
    expect(within(stats).getByText("1:35")).toBeInTheDocument();

    // Cada nome linka para o detalhe do lutador.
    expect(
      within(stats).getByRole("link", { name: /alex pereira/i }),
    ).toHaveAttribute("href", "/fighters/100");
    expect(
      within(stats).getByRole("link", { name: /jamahal hill/i }),
    ).toHaveAttribute("href", "/fighters/200");
  });

  it("destaca o canto vencedor cruzando winner_id com fighter_id", async () => {
    renderBoutPage(7);

    const winner = await screen.findByRole("link", {
      name: /alex pereira, vencedor/i,
    });
    expect(winner).toHaveAttribute("href", "/fighters/100");

    // O perdedor não recebe a marca de vencedor.
    expect(
      screen.getByRole("link", { name: /jamahal hill/i }),
    ).not.toHaveAccessibleName(/vencedor/i);
  });

  it("não destaca nenhum canto e rotula 'sem resultado' num no contest", async () => {
    renderBoutPage(8);

    const result = await screen.findByRole("group", {
      name: /resultado da luta/i,
    });
    expect(within(result).getByText(/sem resultado/i)).toBeInTheDocument();

    expect(
      screen.getByRole("link", { name: /charles oliveira/i }),
    ).not.toHaveAccessibleName(/vencedor/i);
    expect(
      screen.getByRole("link", { name: /arman tsarukyan/i }),
    ).not.toHaveAccessibleName(/vencedor/i);
  });

  it("rotula 'empate' e não destaca nenhum canto quando winner_id é nulo (empate)", async () => {
    renderBoutPage(9);

    const result = await screen.findByRole("group", {
      name: /resultado da luta/i,
    });
    expect(within(result).getByText(/empate/i)).toBeInTheDocument();

    expect(
      screen.getByRole("link", { name: /max holloway/i }),
    ).not.toHaveAccessibleName(/vencedor/i);
    expect(
      screen.getByRole("link", { name: /dustin poirier/i }),
    ).not.toHaveAccessibleName(/vencedor/i);
  });

  it("mostra traço nas stats nulas de um canto", async () => {
    renderBoutPage(8);

    const stats = await screen.findByRole("table", {
      name: /estatísticas por lutador/i,
    });
    // Oliveira (canto vermelho) veio sem box-score: tempo de controle nulo -> traço.
    const controlRow = within(stats).getByRole("row", {
      name: /tempo de controle/i,
    });
    expect(within(controlRow).getByText("—")).toBeInTheDocument();
    // Tsarukyan (canto azul) tem 140s de controle -> 2:20.
    expect(within(controlRow).getByText("2:20")).toBeInTheDocument();
  });

  it("mostra 'luta não encontrada' quando a luta não existe (404)", async () => {
    renderBoutPage(999);

    expect(await screen.findByText(/luta não encontrada/i)).toBeInTheDocument();
  });

  it("mostra 'luta não encontrada' quando o id da rota é inválido", async () => {
    renderBoutPage("abc");

    expect(await screen.findByText(/luta não encontrada/i)).toBeInTheDocument();
  });

  it("mostra mensagem legível quando o backend falha", async () => {
    server.use(
      http.get("*/api/v1/bouts/:id", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderBoutPage(7);

    expect(
      await screen.findByText(/não foi possível carregar a luta/i),
    ).toBeInTheDocument();
  });

  it("navega do card do evento para a página da luta ao clicar em 'ver luta'", async () => {
    const user = userEvent.setup();
    const { router } = renderWithProviders(<EventPage />, {
      routes: [
        { path: "/events/:id", element: <EventPage /> },
        { path: "/bouts/:id", element: <BoutDetail /> },
      ],
      initialEntries: ["/events/42"],
    });

    const bout = await screen.findByRole("listitem", {
      name: /alex pereira vs jamahal hill/i,
    });
    await user.click(within(bout).getByRole("link", { name: /ver luta/i }));

    expect(router.state.location.pathname).toBe("/bouts/7");
    expect(
      await screen.findByRole("table", { name: /estatísticas por lutador/i }),
    ).toBeInTheDocument();
  });
});
