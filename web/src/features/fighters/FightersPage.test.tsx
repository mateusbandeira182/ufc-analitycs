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

  it("renderiza os lutadores vindos da API numa tabela, com uma linha cada", async () => {
    renderFightersPage();

    const tabela = await screen.findByRole("table", { name: /lutadores/i });
    expect(within(tabela).getByText("Jon Jones")).toBeInTheDocument();
    expect(
      within(tabela).getByText("Alexander Volkanovski"),
    ).toBeInTheDocument();
    expect(within(tabela).getByText("Israel Adesanya")).toBeInTheDocument();

    // Uma linha de cabeçalho + uma por lutador.
    expect(within(tabela).getAllByRole("row")).toHaveLength(4);
  });

  it("exibe as colunas de cartel, altura, alcance e base", async () => {
    renderFightersPage();

    const tabela = await screen.findByRole("table", { name: /lutadores/i });
    for (const coluna of [
      /lutador/i,
      /cartel/i,
      /altura/i,
      /alcance/i,
      /base/i,
    ]) {
      expect(
        within(tabela).getByRole("columnheader", { name: coluna }),
      ).toBeInTheDocument();
    }
  });

  it("mostra o cartel, as medidas e a base formatados de cada lutador", async () => {
    renderFightersPage();

    const tabela = await screen.findByRole("table", { name: /lutadores/i });
    const linha = within(tabela)
      .getByText("Jon Jones")
      .closest("tr") as HTMLElement;

    expect(within(linha).getByText("27-1-0")).toBeInTheDocument();
    expect(within(linha).getByText("193 cm")).toBeInTheDocument();
    expect(within(linha).getByText("215 cm")).toBeInTheDocument();
    expect(within(linha).getByText("Ortodoxa")).toBeInTheDocument();
  });

  it("mostra o apelido na linha, tornando a busca por apelido visível na tela", async () => {
    renderFightersPage();

    const tabela = await screen.findByRole("table", { name: /lutadores/i });
    expect(within(tabela).getByText(/Bones/)).toBeInTheDocument();
  });

  it("leva ao lutador pelo link da linha", async () => {
    renderFightersPage();

    const link = await screen.findByRole("link", { name: /jon jones/i });
    expect(link).toHaveAttribute("href", "/fighters/1");
  });

  it("usa o retrato quando há headshot_url e as iniciais quando não há", async () => {
    renderFightersPage();

    const tabela = await screen.findByRole("table", { name: /lutadores/i });

    // Jon Jones tem retrato nas fixtures; Israel Adesanya, não (CA-22).
    expect(
      within(tabela).getByRole("img", { name: "Jon Jones" }),
    ).toBeInTheDocument();
    expect(within(tabela).getByText("IA")).toBeInTheDocument();
    expect(
      within(tabela).queryByRole("img", { name: "Israel Adesanya" }),
    ).not.toBeInTheDocument();
  });

  it("mostra estado vazio quando a busca não retorna resultado", async () => {
    renderFightersPage("/fighters?q=ninguem");

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

  it("filtra server-side ao digitar e reflete o termo na URL como `q`", async () => {
    const user = userEvent.setup();
    const { router } = renderFightersPage();

    await screen.findByText("Jon Jones");

    const search = screen.getByLabelText(/buscar lutador/i);
    await user.type(search, "volkanovski");

    await waitFor(() => {
      expect(screen.queryByText("Jon Jones")).not.toBeInTheDocument();
    });

    const tabela = await screen.findByRole("table", { name: /lutadores/i });
    expect(
      within(tabela).getByText("Alexander Volkanovski"),
    ).toBeInTheDocument();
    expect(within(tabela).queryByText("Jon Jones")).not.toBeInTheDocument();
    expect(router.state.location.search).toContain("q=volkanovski");
  });

  it("encontra o lutador buscando pelo apelido", async () => {
    const user = userEvent.setup();
    const { router } = renderFightersPage();

    await screen.findByText("Jon Jones");

    const search = screen.getByLabelText(/buscar lutador/i);
    await user.type(search, "bones");

    await waitFor(() => {
      expect(
        screen.queryByText("Alexander Volkanovski"),
      ).not.toBeInTheDocument();
    });

    const tabela = await screen.findByRole("table", { name: /lutadores/i });
    expect(within(tabela).getByText("Jon Jones")).toBeInTheDocument();
    expect(router.state.location.search).toContain("q=bones");
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
    expect(router.state.location.search).toContain("q=jon");
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
