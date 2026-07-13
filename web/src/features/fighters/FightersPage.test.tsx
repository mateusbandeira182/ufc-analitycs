import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse, delay } from "msw";
import { describe, expect, it } from "vitest";

import { FightersPage } from "@/features/fighters/FightersPage";
import { server } from "@/mocks/server";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderFightersPage(initialEntry = "/fighters") {
  return renderWithProviders(<FightersPage />, {
    routes: [{ path: "/fighters", element: <FightersPage /> }],
    initialEntries: [initialEntry],
  });
}

describe("FightersPage", () => {
  it("mostra o skeleton de carregamento enquanto os dados chegam", () => {
    server.use(
      http.get("*/api/v1/fighters", async () => {
        await delay("infinite");
        return HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 });
      }),
    );

    renderFightersPage();

    expect(screen.getByTestId("fighters-loading")).toBeInTheDocument();
  });

  it("renderiza os lutadores vindos da API no sucesso", async () => {
    renderFightersPage();

    expect(await screen.findByText("Jon Jones")).toBeInTheDocument();
    expect(screen.getByText("Alexander Volkanovski")).toBeInTheDocument();
    expect(screen.getByText("Israel Adesanya")).toBeInTheDocument();
  });

  it("mostra estado vazio quando a busca não retorna resultado", async () => {
    renderFightersPage("/fighters?name=ninguem");

    expect(
      await screen.findByText(/nenhum lutador encontrado/i),
    ).toBeInTheDocument();
  });

  it("mostra mensagem legível de erro quando o backend falha", async () => {
    server.use(
      http.get("*/api/v1/fighters", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderFightersPage();

    expect(
      await screen.findByText(/não foi possível carregar os lutadores/i),
    ).toBeInTheDocument();
  });

  it("filtra server-side ao digitar e reflete o termo na URL", async () => {
    const user = userEvent.setup();
    const { router } = renderFightersPage();

    await screen.findByText("Jon Jones");

    const search = screen.getByLabelText(/buscar lutador/i);
    await user.type(search, "volkanovski");

    await waitFor(() => {
      expect(screen.queryByText("Jon Jones")).not.toBeInTheDocument();
    });

    const list = await screen.findByRole("list", { name: /lutadores/i });
    expect(within(list).getByText("Alexander Volkanovski")).toBeInTheDocument();
    expect(within(list).queryByText("Jon Jones")).not.toBeInTheDocument();
    expect(router.state.location.search).toContain("name=volkanovski");
  });

  it("pagina server-side lendo limit/offset da URL", async () => {
    renderFightersPage("/fighters?limit=2");

    await screen.findByText("Jon Jones");
    expect(screen.getByText("Alexander Volkanovski")).toBeInTheDocument();
    expect(screen.queryByText("Israel Adesanya")).not.toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: /paginação de lutadores/i }),
    ).toHaveTextContent(/página 1 de 2/i);
    expect(screen.getByText(/anterior/i)).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByRole("link", { name: /próxima/i })).toBeInTheDocument();
  });

  it("avança de página mudando o offset na URL", async () => {
    const user = userEvent.setup();
    const { router } = renderFightersPage("/fighters?limit=2");

    await screen.findByText("Jon Jones");
    await user.click(screen.getByRole("link", { name: /próxima/i }));

    expect(router.state.location.search).toContain("offset=2");
    expect(await screen.findByText("Israel Adesanya")).toBeInTheDocument();
    expect(screen.getByText(/próxima/i)).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("reseta o offset ao mudar o termo de busca", async () => {
    const user = userEvent.setup();
    const { router } = renderFightersPage("/fighters?limit=2&offset=2");

    await screen.findByText("Israel Adesanya");

    const search = screen.getByLabelText(/buscar lutador/i);
    await user.type(search, "jon");

    await waitFor(() => {
      expect(router.state.location.search).not.toContain("offset");
    });
    expect(router.state.location.search).toContain("name=jon");
  });

  it("orienta o usuário quando a página está fora do intervalo", async () => {
    renderFightersPage("/fighters?limit=2&offset=10");

    expect(
      await screen.findByText(/página fora do intervalo/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /primeira página/i }),
    ).toBeInTheDocument();
  });
});
