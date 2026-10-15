import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AppLayout } from "@/components/layout/AppLayout";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderLayout() {
  return renderWithProviders(<div>Conteúdo da rota</div>, {
    routes: [
      {
        element: <AppLayout />,
        children: [
          { index: true, element: <div>Conteúdo da rota</div> },
          { path: "/fighters", element: <div>Rota de lutadores</div> },
        ],
      },
    ],
    initialEntries: ["/"],
  });
}

describe("AppLayout", () => {
  it("renderiza a navegação principal com os três destinos", () => {
    renderLayout();

    const nav = screen.getByRole("navigation", { name: /principal/i });
    expect(
      within(nav).getByRole("link", { name: /lutadores/i }),
    ).toHaveAttribute("href", "/fighters");
    expect(within(nav).getByRole("link", { name: /eventos/i })).toHaveAttribute(
      "href",
      "/events",
    );
    expect(
      within(nav).getByRole("link", { name: /confronto/i }),
    ).toHaveAttribute("href", "/confronto");
  });

  it("renderiza o conteúdo da rota filha no outlet", () => {
    renderLayout();

    expect(screen.getByText("Conteúdo da rota")).toBeInTheDocument();
  });
});

describe("AppLayout — busca permanente no cabeçalho", () => {
  it("mantém a busca no cabeçalho em toda rota", () => {
    renderLayout();

    const banner = screen.getByRole("banner");
    expect(
      within(banner).getByLabelText(/buscar lutador/i),
    ).toBeInTheDocument();
  });

  it("submeter a busca leva a /fighters propagando o termo como `q`", async () => {
    const user = userEvent.setup();
    const { router } = renderLayout();

    const banner = screen.getByRole("banner");
    await user.type(within(banner).getByLabelText(/buscar lutador/i), "jones");
    await user.keyboard("{Enter}");

    expect(await screen.findByText("Rota de lutadores")).toBeInTheDocument();
    expect(router.state.location.search).toContain("q=jones");
  });

  it("leva ao acervo completo quando a busca está vazia", async () => {
    const user = userEvent.setup();
    const { router } = renderLayout();

    const banner = screen.getByRole("banner");
    await user.click(within(banner).getByLabelText(/buscar lutador/i));
    await user.keyboard("{Enter}");

    expect(router.state.location.pathname).toBe("/fighters");
    expect(router.state.location.search).toBe("");
  });
});
