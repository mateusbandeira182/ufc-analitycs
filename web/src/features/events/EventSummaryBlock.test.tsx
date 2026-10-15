import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EventSummaryOut } from "@/api/schema";
import { EventSummaryBlock } from "@/features/events/EventSummaryBlock";

const RESUMO: EventSummaryOut = {
  n_bouts: 13,
  ko_tko: 4,
  submissions: 3,
  decisions: 5,
  draws: 0,
  no_contests: 0,
  dq: 1,
};

/**
 * O contêiner de um balde, achado pelo texto do `<dt>`. `getByRole("term")` não
 * serve: `dt` não computa nome acessível a partir do conteúdo.
 */
function bucket(label: RegExp): HTMLElement {
  const dt = screen.getByText(label);
  const item = dt.parentElement;
  if (item === null) {
    throw new Error(`Balde sem contêiner: ${String(label)}`);
  }
  return item;
}

describe("EventSummaryBlock", () => {
  it("mostra a contagem por método com o denominador na tela", () => {
    render(<EventSummaryBlock summary={RESUMO} />);

    // O denominador acompanha a contagem para que um card misto nunca se leia
    // como o card inteiro.
    expect(screen.getByText(/13 lutas realizadas/i)).toBeInTheDocument();

    expect(bucket(/ko\/tko/i)).toHaveTextContent("4");
  });

  it("exibe todos os seis baldes, inclusive desqualificação", () => {
    render(<EventSummaryBlock summary={RESUMO} />);

    for (const rotulo of [
      /ko\/tko/i,
      /finalizaç/i,
      /decis/i,
      /empate/i,
      /sem resultado/i,
      /desqualificaç/i,
    ]) {
      expect(bucket(rotulo)).toBeInTheDocument();
    }
  });

  it("mostra zero MEDIDO como zero, porque é medição", () => {
    render(<EventSummaryBlock summary={RESUMO} />);

    expect(bucket(/empate/i)).toHaveTextContent("0");
  });

  it("não renderiza nada quando `summary` é nulo — ausência não é zero", () => {
    const { container } = render(<EventSummaryBlock summary={null} />);

    // Zero finalização é uma afirmação sobre o evento; ausência de resultado
    // não é zero. Com `summary` nulo não há bloco e não há dígito na tela.
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });
});
