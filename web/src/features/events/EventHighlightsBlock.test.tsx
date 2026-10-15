import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EventHighlightsOut } from "@/api/schema";
import { EventHighlightsBlock } from "@/features/events/EventHighlightsBlock";
import { renderWithProviders } from "@/test/renderWithProviders";

const DESTAQUES: EventHighlightsOut = {
  fastest_finish: {
    bout_id: 9,
    fighter: { id: 500, name: "Bo Nickal" },
    method: "submission",
    round: 1,
    ending_time_seconds: 230,
  },
  most_sig_strikes: {
    bout_id: 7,
    fighter: { id: 100, name: "Alex Pereira" },
    value: 42,
  },
  most_takedowns: {
    bout_id: 9,
    fighter: { id: 500, name: "Bo Nickal" },
    value: 3,
  },
  biggest_upset: {
    bout_id: 9,
    winner: { id: 500, name: "Bo Nickal" },
    prob_of_winner: 0.28,
    n_bouts_with_prediction: 9,
    n_bouts: 13,
  },
};

/**
 * O contêiner de um destaque, achado pelo texto do `<dt>`. `getByRole("term")`
 * não serve: `dt` não computa nome acessível a partir do conteúdo.
 */
function highlight(label: RegExp): HTMLElement {
  const dt = screen.getByText(label);
  const item = dt.parentElement;
  if (item === null) {
    throw new Error(`Destaque sem contêiner: ${String(label)}`);
  }
  return item;
}

describe("EventHighlightsBlock", () => {
  it("rotula o conjunto como calculado, não oficial", () => {
    renderWithProviders(<EventHighlightsBlock highlights={DESTAQUES} />);

    // Nenhum destaque é premiação: `awards` da Cito é sempre nulo e a fonte
    // oficial não publica. A tela não pode insinuar que são oficiais.
    expect(screen.getByText(/calculado, não oficial/i)).toBeInTheDocument();
  });

  it("mostra a finalização mais rápida como round e tempo DENTRO do round", () => {
    renderWithProviders(<EventHighlightsBlock highlights={DESTAQUES} />);

    // `ending_time_seconds` é o tempo dentro do round; não existe total
    // decorrido no contrato, e fabricá-lo exigiria assumir 300s por round.
    expect(screen.getByText("R1 3:50")).toBeInTheDocument();
    expect(screen.getAllByText(/bo nickal/i).length).toBeGreaterThan(0);
  });

  it("mostra o volume de golpes e de quedas com o lutador", () => {
    renderWithProviders(<EventHighlightsBlock highlights={DESTAQUES} />);

    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(/alex pereira/i)).toBeInTheDocument();
  });

  it("mostra a maior zebra com o denominador declarado", () => {
    renderWithProviders(<EventHighlightsBlock highlights={DESTAQUES} />);

    expect(screen.getByText("28%")).toBeInTheDocument();
    expect(screen.getByText(/9 de 13 lutas previstas/i)).toBeInTheDocument();
  });

  it("mostra `value: 0` como 0, porque zero medido é medição", () => {
    renderWithProviders(
      <EventHighlightsBlock
        highlights={{
          ...DESTAQUES,
          most_takedowns: {
            bout_id: 9,
            fighter: { id: 500, name: "Bo Nickal" },
            value: 0,
          },
        }}
      />,
    );

    expect(highlight(/quedas/i)).toHaveTextContent("0");
  });

  it("omite o destaque nulo em vez de exibir zero ou traço mudo", () => {
    renderWithProviders(
      <EventHighlightsBlock
        highlights={{
          ...DESTAQUES,
          most_takedowns: null,
          biggest_upset: null,
        }}
      />,
    );

    expect(screen.queryByText(/quedas/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/zebra/i)).not.toBeInTheDocument();
    // Os que têm base seguem na tela.
    expect(highlight(/finalização mais rápida/i)).toBeInTheDocument();
  });

  it("não renderiza nada quando `highlights` é nulo", () => {
    const { container } = renderWithProviders(
      <EventHighlightsBlock highlights={null} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("não renderiza nada quando todos os destaques são nulos", () => {
    const { container } = renderWithProviders(
      <EventHighlightsBlock
        highlights={{
          fastest_finish: null,
          most_sig_strikes: null,
          most_takedowns: null,
          biggest_upset: null,
        }}
      />,
    );

    // Um cabeçalho "Destaques" sobre lista vazia seria bloco vazio e mudo.
    expect(container).toBeEmptyDOMElement();
  });
});
