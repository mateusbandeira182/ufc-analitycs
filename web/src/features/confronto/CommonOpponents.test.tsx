import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CommonOpponentOut } from "@/api/schema";
import { CommonOpponents } from "@/features/confronto/CommonOpponents";
import { JONES_ADESANYA_COMMON } from "@/mocks/matchup";

/*
  As lutas do lado A da fixture. Estourar aqui é falha de fixture: um `?? []`
  deixaria o caso de "um lado só" passar com lista vazia dos dois lados, isto é,
  passar sem exercitar nada.
*/
function boutsDoLadoA() {
  const [primeiro] = JONES_ADESANYA_COMMON;
  if (!primeiro) {
    throw new Error("Fixture de adversário em comum vazia.");
  }
  return primeiro.bouts_a;
}
import { renderWithProviders } from "@/test/renderWithProviders";

/* Os dois lados do confronto, com id: o id é o que decide o desfecho de cada luta. */
const SIDE_A = { id: 1, name: "Jon Jones" };
const SIDE_B = { id: 3, name: "Israel Adesanya" };
const NAME_A = SIDE_A.name;
const NAME_B = SIDE_B.name;

function renderAdversarios(opponents = JONES_ADESANYA_COMMON) {
  return renderWithProviders(
    <CommonOpponents opponents={opponents} sideA={SIDE_A} sideB={SIDE_B} />,
  );
}

/** O bloco de um adversário em comum, pelo rótulo acessível. */
function bloco(label: RegExp): HTMLElement {
  return screen.getByRole("listitem", { name: label });
}

describe("CommonOpponents — o adversário e o desfecho de cada lado", () => {
  it("identifica o adversário em comum com link para a página dele", () => {
    renderAdversarios();
    const alexander = bloco(/alexander volkanovski/i);

    expect(
      within(alexander).getByRole("link", { name: /alexander volkanovski/i }),
    ).toHaveAttribute("href", "/fighters/2");
  });

  it("separa o desfecho de cada lado contra o mesmo terceiro", () => {
    renderAdversarios();
    const alexander = bloco(/alexander volkanovski/i);

    // O lado A venceu; o lado B tem duas lutas, uma perdida e uma sem vencedor.
    const ladoA = within(alexander).getByRole("list", {
      name: new RegExp(NAME_A, "i"),
    });
    const ladoB = within(alexander).getByRole("list", {
      name: new RegExp(NAME_B, "i"),
    });

    expect(within(ladoA).getByText(/venceu/i)).toBeInTheDocument();
    expect(within(ladoA).getAllByRole("listitem")).toHaveLength(1);
    expect(within(ladoB).getAllByRole("listitem")).toHaveLength(2);
  });

  it("lista cada luta do mesmo lado contra o mesmo adversário em sua própria linha", () => {
    renderAdversarios();

    expect(screen.getByRole("listitem", { name: /ufc 243/i })).toBeTruthy();
    expect(screen.getByRole("listitem", { name: /ufc 281/i })).toBeTruthy();
  });
});

describe("CommonOpponents — empate não colapsa em derrota", () => {
  it("rotula vitória, derrota e empate como três desfechos distintos", () => {
    /*
      `winner_id` nulo é empate ou no contest. Colapsá-lo em derrota é o defeito
      que esta SPA já cometeu uma vez: "perdeu para X" e "empatou com X" não podem
      virar a mesma afirmação justamente nesta seção, onde os dois lutadores são
      comparados contra o MESMO terceiro.
    */
    renderAdversarios();

    const ufc200 = bloco(/ufc 200/i);
    const ufc243 = bloco(/ufc 243/i);
    const ufc281 = bloco(/ufc 281/i);

    expect(within(ufc200).getByText(/venceu/i)).toBeInTheDocument();
    expect(within(ufc243).getByText(/perdeu/i)).toBeInTheDocument();
    expect(within(ufc281).getByText(/sem vencedor|empat/i)).toBeInTheDocument();

    // A luta sem vencedor não é rotulada como derrota.
    expect(within(ufc281).queryByText(/perdeu/i)).not.toBeInTheDocument();
    expect(within(ufc243).queryByText(/venceu/i)).not.toBeInTheDocument();
  });

  it("exibe a data e o método de cada luta, formatados", () => {
    renderAdversarios();
    const ufc243 = bloco(/ufc 243/i);

    expect(within(ufc243).getByText("06/10/2019")).toBeInTheDocument();
    expect(within(ufc243).getByText(/decisão/i)).toBeInTheDocument();
  });
});

describe("CommonOpponents — lista vazia", () => {
  it("tem estado próprio e explícito quando não há adversário em comum", () => {
    // Estado próprio, não seção vazia sem explicação: a dupla pode não ter
    // adversário em comum e isso é informação.
    renderAdversarios([]);

    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText(/nenhum adversário em comum/i)).toBeInTheDocument();
  });

  it("não lista luta alguma quando um dos lados não enfrentou o adversário", () => {
    const soLadoA: CommonOpponentOut[] = [
      {
        opponent: { id: 2, name: "Alexander Volkanovski" },
        bouts_a: boutsDoLadoA(),
        bouts_b: [],
      },
    ];
    renderAdversarios(soLadoA);

    const alexander = bloco(/alexander volkanovski/i);
    expect(within(alexander).getByText(/não enfrentou/i)).toBeInTheDocument();
  });
});
