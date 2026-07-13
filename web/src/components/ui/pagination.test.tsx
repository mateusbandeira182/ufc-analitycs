import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Pagination } from "@/components/ui/pagination";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderPagination(
  props: { total: number; limit: number; offset: number; label: string },
  initialEntry = "/fighters",
) {
  return renderWithProviders(<Pagination {...props} />, {
    routes: [{ path: "/fighters", element: <Pagination {...props} /> }],
    initialEntries: [initialEntry],
  });
}

describe("Pagination", () => {
  it("mostra a página atual e o total de páginas", () => {
    renderPagination({ total: 50, limit: 10, offset: 0, label: "lutadores" });

    expect(screen.getByText(/página 1 de 5/i)).toBeInTheDocument();
  });

  it("desabilita 'anterior' na primeira página e oferece 'próxima' como link", () => {
    renderPagination({ total: 50, limit: 10, offset: 0, label: "lutadores" });

    const previous = screen.getByText(/anterior/i);
    expect(previous).toHaveAttribute("aria-disabled", "true");
    expect(previous).not.toHaveAttribute("href");

    const next = screen.getByRole("link", { name: /próxima/i });
    expect(next).toHaveAttribute("href", "/fighters?offset=10");
  });

  it("desabilita 'próxima' na última página e oferece 'anterior' como link", () => {
    renderPagination({ total: 50, limit: 10, offset: 40, label: "lutadores" });

    const next = screen.getByText(/próxima/i);
    expect(next).toHaveAttribute("aria-disabled", "true");
    expect(next).not.toHaveAttribute("href");

    const previous = screen.getByRole("link", { name: /anterior/i });
    expect(previous).toHaveAttribute("href", "/fighters?offset=30");
  });

  it("preserva os demais parâmetros da URL ao mudar de página", () => {
    renderPagination(
      { total: 50, limit: 10, offset: 0, label: "lutadores" },
      "/fighters?name=jones",
    );

    const next = screen.getByRole("link", { name: /próxima/i });
    expect(next).toHaveAttribute("href", "/fighters?name=jones&offset=10");
  });

  it("remove o parâmetro offset ao voltar para a primeira página", () => {
    renderPagination({ total: 50, limit: 10, offset: 10, label: "lutadores" });

    const previous = screen.getByRole("link", { name: /anterior/i });
    expect(previous).toHaveAttribute("href", "/fighters");
  });

  it("navega para a próxima página alterando o offset na URL", async () => {
    const user = userEvent.setup();
    const { router } = renderPagination({
      total: 50,
      limit: 10,
      offset: 0,
      label: "lutadores",
    });

    await user.click(screen.getByRole("link", { name: /próxima/i }));

    expect(router.state.location.search).toBe("?offset=10");
  });
});
