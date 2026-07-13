import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { FighterPage } from "@/features/fighters/FighterPage";
import { FightersPage } from "@/features/fighters/FightersPage";
import { BOUTS_WITH_NULL_OPPONENT } from "@/mocks/bouts";
import { server } from "@/mocks/server";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderFighterPage(id: number | string) {
  return renderWithProviders(<FighterPage />, {
    routes: [{ path: "/fighters/:id", element: <FighterPage /> }],
    initialEntries: [`/fighters/${String(id)}`],
  });
}

describe("FighterPage", () => {
  it("exibe o nome e o cartel V/D/E do lutador no sucesso", async () => {
    renderFighterPage(1);

    expect(
      await screen.findByRole("heading", { name: /jon jones/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("27-1-0")).toBeInTheDocument();
  });

  it("exibe o peso do lutador no cabeçalho", async () => {
    renderFighterPage(1);

    // Jon Jones (fixture): 93 kg.
    expect(await screen.findByText("Peso")).toBeInTheDocument();
    expect(screen.getByText("93 kg")).toBeInTheDocument();
  });

  it("exibe o histórico em ordem cronológica com evento, data, adversário e resultado", async () => {
    renderFighterPage(1);

    const history = await screen.findByRole("list", { name: /histórico/i });
    const rows = within(history).getAllByRole("listitem");

    // A ordem cronológica é a que o backend entrega — o cliente não reordena.
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("UFC 152");
    expect(rows[0]).toHaveTextContent("22/09/2012");
    expect(rows[0]).toHaveTextContent("Alexander Volkanovski");
    expect(rows[0]).toHaveTextContent("Derrota");
    expect(rows[0]).toHaveTextContent("Decisão");

    expect(rows[1]).toHaveTextContent("UFC 200");
    expect(rows[1]).toHaveTextContent("09/07/2016");
    expect(rows[1]).toHaveTextContent("Israel Adesanya");
    expect(rows[1]).toHaveTextContent("Vitória");
    expect(rows[1]).toHaveTextContent("KO/TKO");
  });

  it("linka cada luta para /bouts/:id e o adversário para /fighters/:id", async () => {
    renderFighterPage(1);

    const boutLink = await screen.findByRole("link", { name: /ufc 200/i });
    expect(boutLink).toHaveAttribute("href", "/bouts/92");

    const opponentLink = screen.getByRole("link", {
      name: /israel adesanya/i,
    });
    expect(opponentLink).toHaveAttribute("href", "/fighters/3");
  });

  it("trata adversário nulo com texto de fallback e sem link", async () => {
    server.use(
      http.get("*/api/v1/fighters/:id/bouts", () =>
        HttpResponse.json(BOUTS_WITH_NULL_OPPONENT),
      ),
    );

    renderFighterPage(1);

    expect(
      await screen.findByText(/adversário desconhecido/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /adversário desconhecido/i }),
    ).not.toBeInTheDocument();
  });

  it("navega da lista para o detalhe ao clicar num lutador", async () => {
    const user = userEvent.setup();
    const { router } = renderWithProviders(<FightersPage />, {
      routes: [
        { path: "/fighters", element: <FightersPage /> },
        { path: "/fighters/:id", element: <FighterPage /> },
      ],
      initialEntries: ["/fighters"],
    });

    const link = await screen.findByRole("link", { name: /jon jones/i });
    await user.click(link);

    expect(router.state.location.pathname).toBe("/fighters/1");
    expect(
      await screen.findByRole("heading", { name: /jon jones/i }),
    ).toBeInTheDocument();
  });

  it("mostra o skeleton enquanto o lutador carrega", () => {
    server.use(
      http.get("*/api/v1/fighters/:id", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    renderFighterPage(1);

    expect(screen.getByLabelText(/carregando lutador/i)).toBeInTheDocument();
  });

  it("mostra mensagem legível quando o backend falha", async () => {
    server.use(
      http.get("*/api/v1/fighters/:id", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderFighterPage(1);

    expect(
      await screen.findByText(/não foi possível carregar o lutador/i),
    ).toBeInTheDocument();
  });

  it("mostra 'não encontrado' quando o lutador não existe (404)", async () => {
    renderFighterPage(999);

    expect(
      await screen.findByText(/lutador não encontrado/i),
    ).toBeInTheDocument();
  });

  it("trata id de rota inválido como não encontrado, sem disparar request", async () => {
    // Ex.: `/fighters/abc` -> Number("abc") é NaN. O hook fica desabilitado e a
    // página mostra "não encontrado", coerente com /events e /bouts.
    let requested = false;
    server.use(
      http.get("*/api/v1/fighters/:id", () => {
        requested = true;
        return HttpResponse.json({ detail: "Not Found" }, { status: 404 });
      }),
    );

    renderFighterPage("abc");

    expect(
      await screen.findByText(/lutador não encontrado/i),
    ).toBeInTheDocument();
    expect(requested).toBe(false);
  });

  it("mostra mensagem própria quando o histórico está vazio, mantendo o cartel", async () => {
    server.use(
      http.get("*/api/v1/fighters/:id/bouts", () => HttpResponse.json([])),
    );

    renderFighterPage(1);

    expect(
      await screen.findByText(/nenhuma luta registrada/i),
    ).toBeInTheDocument();
    expect(screen.getByText("27-1-0")).toBeInTheDocument();
  });
});
