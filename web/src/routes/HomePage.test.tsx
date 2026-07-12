import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { HomePage } from "@/routes/HomePage";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderHome() {
  return renderWithProviders(<HomePage />, {
    routes: [
      { path: "/", element: <HomePage /> },
      { path: "/fighters", element: <div>Rota de lutadores</div> },
    ],
    initialEntries: ["/"],
  });
}

describe("HomePage", () => {
  it("apresenta o hero, a busca em destaque e os cards de navegação", () => {
    renderHome();

    expect(
      screen.getByRole("heading", { level: 1, name: /octógono/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/buscar lutador/i)).toBeInTheDocument();

    expect(
      screen.getByRole("link", { name: /lutadores/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /eventos/i })).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /head-to-head/i }),
    ).toBeInTheDocument();
  });

  it("navega para /fighters propagando o termo buscado como name", async () => {
    const user = userEvent.setup();
    const { router } = renderHome();

    await user.type(screen.getByLabelText(/buscar lutador/i), "adesanya");
    await user.keyboard("{Enter}");

    expect(await screen.findByText("Rota de lutadores")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/fighters");
    expect(router.state.location.search).toContain("name=adesanya");
  });
});
