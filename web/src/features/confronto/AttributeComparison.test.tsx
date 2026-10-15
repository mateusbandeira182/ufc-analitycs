import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MatchupAttributeOut } from "@/api/schema";
import { AttributeComparison } from "@/features/confronto/AttributeComparison";
import { DASH } from "@/lib/format";
import { MATCHUP_ATTRIBUTE_FIXTURES } from "@/mocks/matchup";

const NAME_A = "Jon Jones";
const NAME_B = "Alexander Volkanovski";

function renderAtributos(attributes = MATCHUP_ATTRIBUTE_FIXTURES) {
  return render(
    <AttributeComparison
      attributes={attributes}
      nameA={NAME_A}
      nameB={NAME_B}
    />,
  );
}

/** A linha de um atributo, pelo rótulo acessível. */
function row(label: RegExp | string): HTMLElement {
  return screen.getByRole("listitem", { name: label });
}

describe("AttributeComparison — o rótulo vem do payload", () => {
  it("exibe os oito rótulos que o backend mandou", () => {
    renderAtributos();

    for (const atributo of MATCHUP_ATTRIBUTE_FIXTURES) {
      expect(screen.getByText(atributo.label)).toBeInTheDocument();
    }
    expect(screen.getAllByRole("listitem")).toHaveLength(8);
  });

  it("não redigita o rótulo: exibe o que vier, mesmo um que a tela nunca viu", () => {
    /*
      O rótulo é do payload, nunca da tela. Um mapa chave -> rótulo apodreceria no
      dia em que o backend acrescentasse o nono atributo, e o `label` já carrega a
      unidade (ex.: "... (segundos)").
    */
    const inventado: MatchupAttributeOut = {
      key: "cadencia_de_clinch_r3",
      label: "Cadência de clinch por minuto (inventado na fixture)",
      value_a: 1.25,
      value_b: 2.5,
    };
    renderAtributos([inventado]);

    expect(screen.getByText(inventado.label)).toBeInTheDocument();
  });

  it("nomeia os dois lados da comparação", () => {
    renderAtributos();
    const secao = screen.getByRole("region", { name: /atributos/i });

    expect(within(secao).getByText(NAME_A)).toBeInTheDocument();
    expect(within(secao).getByText(NAME_B)).toBeInTheDocument();
  });
});

describe("AttributeComparison — ausência de valor as-of", () => {
  it("renderiza traço para o lado sem valor, nunca zero", () => {
    /*
      `null` é ausência de medição as-of; `0` seria uma taxa legítima afirmada sem
      medição. Esta é a mesma distinção que o `avg` do Postgres ignorando `NULL`
      já custou a esta SPEC uma vez.
    */
    renderAtributos();
    const linha = row(/precisão de quedas/i);

    expect(within(linha).getByText(DASH)).toBeInTheDocument();
    expect(within(linha).queryByText("0,00")).not.toBeInTheDocument();
  });

  it("preserva o zero medido do outro lado da mesma seção", () => {
    // `finish_rate_prior` do lado B é zero de verdade: mediu e deu zero.
    renderAtributos();
    const linha = row(/taxa de finalização/i);

    expect(within(linha).getByText("0,00")).toBeInTheDocument();
    expect(within(linha).queryByText(DASH)).not.toBeInTheDocument();
  });

  it("exibe os valores formatados com duas casas em pt-BR", () => {
    renderAtributos();
    const linha = row(/tempo de controle médio/i);

    expect(within(linha).getByText("214,50")).toBeInTheDocument();
    expect(within(linha).getByText("88,25")).toBeInTheDocument();
  });
});

describe("AttributeComparison — lista vazia", () => {
  it("não renderiza a seção quando a resposta não trouxe atributo algum", () => {
    // No estado indisponível o contrato manda lista VAZIA, não lista de nulos.
    renderAtributos([]);

    expect(
      screen.queryByRole("region", { name: /atributos/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });
});
