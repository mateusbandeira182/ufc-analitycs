import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DirectBouts } from "@/features/confronto/DirectBouts";
import { headToHeadBouts } from "@/mocks/headToHead";
import { renderWithProviders } from "@/test/renderWithProviders";

const NAME_A = "Jon Jones";
const NAME_B = "Alexander Volkanovski";

function renderConfronto(bouts = headToHeadBouts(1, 2)) {
  return renderWithProviders(
    <DirectBouts bouts={bouts} nameA={NAME_A} nameB={NAME_B} />,
  );
}

describe("DirectBouts — a lista do confronto direto", () => {
  it("lista o confronto com o vencedor, o método e o momento", () => {
    renderConfronto();
    const luta = screen.getByRole("listitem", { name: /ufc 200/i });

    expect(within(luta).getByText("09/07/2016")).toBeInTheDocument();
    expect(within(luta).getByText("KO/TKO")).toBeInTheDocument();
    // Jon Jones (`winner_id` 1) venceu.
    expect(within(luta).getByText(/jon jones/i)).toBeInTheDocument();
  });

  it("leva ao detalhe da luta", () => {
    renderConfronto();
    const luta = screen.getByRole("listitem", { name: /ufc 200/i });

    expect(
      within(luta).getByRole("link", { name: /ver luta/i }),
    ).toHaveAttribute("href", "/bouts/500");
  });
});

describe("DirectBouts — nunca se enfrentaram", () => {
  it("nomeia os dois atletas ao dizer que não há confronto registrado", () => {
    // Lista vazia com os dois existentes é 200, distinto do 404: a ausência de
    // confronto é informação, não falha.
    renderConfronto([]);

    expect(screen.getByText(/nunca se enfrentaram/i)).toBeInTheDocument();
    expect(
      screen.getByText(new RegExp(`${NAME_A}.*${NAME_B}`)),
    ).toBeInTheDocument();
  });

  it("não lista luta alguma quando nunca se enfrentaram", () => {
    renderConfronto([]);

    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });
});
