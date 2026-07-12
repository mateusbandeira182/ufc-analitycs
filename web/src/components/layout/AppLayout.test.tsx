import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AppLayout } from "@/components/layout/AppLayout";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderLayout() {
  return renderWithProviders(<div>Conteúdo da rota</div>, {
    routes: [
      {
        element: <AppLayout />,
        children: [{ index: true, element: <div>Conteúdo da rota</div> }],
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
      within(nav).getByRole("link", { name: /head-to-head/i }),
    ).toHaveAttribute("href", "/head-to-head");
  });

  it("renderiza o conteúdo da rota filha no outlet", () => {
    renderLayout();

    expect(screen.getByText("Conteúdo da rota")).toBeInTheDocument();
  });
});
