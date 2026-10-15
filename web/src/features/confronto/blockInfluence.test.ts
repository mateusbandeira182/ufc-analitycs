import { describe, expect, it } from "vitest";

import type { MatchupBlockInfluenceOut } from "@/api/schema";
import {
  BLOCK_LABELS,
  barScaleOf,
  blockLabel,
  coverageLabel,
  favoredLabel,
  formatDeltaProbPoints,
  isUnevaluated,
  sortedBlocks,
} from "@/features/confronto/blockInfluence";
import { MATCHUP_BLOCK_FIXTURES } from "@/mocks/matchup";

/** Bloco avulso para os casos que a fixture medida não cobre. */
function block(
  overrides: Partial<MatchupBlockInfluenceOut> = {},
): MatchupBlockInfluenceOut {
  return {
    name: "striking-profile",
    description: "prosa de engenharia",
    n_columns: 18,
    n_columns_evaluated: 18,
    coverage: "full",
    delta_prob_a: 0.041,
    ...overrides,
  };
}

/*
  O sinal de `delta_prob_a` ESCOLHE de quem é o nome exibido na tela (D-11):
  `delta_prob_a = prob_a_base − prob_a_ablado`, e positivo favorece o lado A.
  Por isso os casos abaixo usam valor conhecido nos dois sinais — uma inversão
  aqui mostraria "favorece A" onde o bloco favorece B, sem quebrar mais nada.
*/
describe("formatDeltaProbPoints — deslocamento em pontos percentuais", () => {
  it("formata deslocamento positivo com sinal explícito", () => {
    expect(formatDeltaProbPoints(0.041)).toBe("+4,1 p.p.");
  });

  it("formata deslocamento negativo com sinal explícito", () => {
    expect(formatDeltaProbPoints(-0.041)).toBe("-4,1 p.p.");
  });

  it("não afirma direção quando o deslocamento medido é zero", () => {
    // "+0,0" afirmaria uma direção que a medição não tem.
    expect(formatDeltaProbPoints(0)).toBe("0,0 p.p.");
  });

  it("nunca exibe zero negativo", () => {
    // `(-0).toFixed(1)` é `"-0.0"`: sem tratamento, a tela mostraria "-0,0 p.p.".
    expect(formatDeltaProbPoints(-0)).toBe("0,0 p.p.");
  });

  it("não afirma direção quando o arredondamento apaga o deslocamento", () => {
    // 0,04 p.p. arredonda para "0,0": dizer "+0,0 p.p." seria uma direção que o
    // número exibido não sustenta.
    expect(formatDeltaProbPoints(0.0004)).toBe("0,0 p.p.");
    expect(formatDeltaProbPoints(-0.0004)).toBe("0,0 p.p.");
  });
});

describe("favoredLabel — de quem é o nome exibido", () => {
  it("deslocamento positivo favorece o lado A", () => {
    expect(favoredLabel(0.041, "Jon Jones", "Alexander Volkanovski")).toBe(
      "favorece Jon Jones",
    );
  });

  it("deslocamento negativo favorece o lado B", () => {
    expect(favoredLabel(-0.041, "Jon Jones", "Alexander Volkanovski")).toBe(
      "favorece Alexander Volkanovski",
    );
  });

  it("deslocamento zero não favorece ninguém", () => {
    expect(favoredLabel(0, "Jon Jones", "Alexander Volkanovski")).toBe(
      "sem deslocamento",
    );
  });

  it("não favorece ninguém quando o arredondamento apaga o deslocamento", () => {
    /*
      O rótulo e o número aparecem lado a lado na mesma linha: "0,0 p.p." com
      "favorece Jon Jones" seria uma contradição na tela. Os dois decidem pelo
      MESMO valor arredondado, então nunca divergem.
    */
    expect(favoredLabel(0.0004, "Jon Jones", "Alexander Volkanovski")).toBe(
      "sem deslocamento",
    );
  });
});

describe("blockLabel — rótulo legível do bloco", () => {
  it("traduz os nomes do registro para pt-BR acentuado", () => {
    expect(blockLabel("striking-profile")).toBe("Perfil de striking");
    expect(blockLabel("dinamica-por-round")).toBe("Dinâmica por round");
    expect(blockLabel("dimensoes-ausentes")).toBe("Dimensões ausentes");
    expect(blockLabel("historico-qualificado")).toBe("Histórico qualificado");
    expect(blockLabel("forca-de-calendario")).toBe("Força de calendário");
  });

  it("degrada para o slug humanizado quando o bloco não está mapeado", () => {
    /*
      Fallback deliberadamente VISÍVEL: um bloco que o backend acrescentar e
      ninguém rotular tem de se notar na tela. O mapa decide só rótulo — se
      decidisse presença, o bloco desapareceria em silêncio.
    */
    expect(blockLabel("bloco-que-ninguem-mapeou")).toBe(
      "Bloco que ninguem mapeou",
    );
  });

  it("nunca devolve string vazia para nome desconhecido", () => {
    expect(blockLabel("")).not.toBe("");
  });

  it("tem rótulo para cada um dos onze blocos que o contrato emite", () => {
    /*
      Teste de completude: falha no dia em que o backend acrescentar um bloco à
      fixture e ninguém o rotular. É o que impede o mapa de apodrecer calado.
    */
    const semRotulo = MATCHUP_BLOCK_FIXTURES.filter(
      (entry) => !(entry.name in BLOCK_LABELS),
    ).map((entry) => entry.name);

    expect(semRotulo).toEqual([]);
    expect(MATCHUP_BLOCK_FIXTURES).toHaveLength(11);
  });
});

describe("coverageLabel — derivado das contagens do payload", () => {
  it("declara o denominador quando a cobertura é parcial", () => {
    expect(
      coverageLabel(
        block({ coverage: "partial", n_columns: 5, n_columns_evaluated: 3 }),
      ),
    ).toBe("parcial: 3 de 5 colunas avaliadas");
  });

  it("diz 'não avaliado', nunca 'não influenciou', quando não houve medição", () => {
    // `coverage: "none"` é ausência de medição. A leitura "não influenciou"
    // afirmaria que a divisão não importa, quando ela não foi medida.
    expect(
      coverageLabel(
        block({
          coverage: "none",
          n_columns: 2,
          n_columns_evaluated: 0,
          delta_prob_a: null,
        }),
      ),
    ).toBe("não avaliado");
  });

  it("declara quantas colunas entraram quando a cobertura é total", () => {
    expect(coverageLabel(block({ coverage: "full" }))).toBe(
      "18 colunas avaliadas",
    );
  });

  it("concorda em número quando o bloco tem uma coluna só", () => {
    expect(
      coverageLabel(
        block({ coverage: "full", n_columns: 1, n_columns_evaluated: 1 }),
      ),
    ).toBe("1 coluna avaliada");
  });
});

describe("isUnevaluated — o bloco sem medição", () => {
  it("reconhece o bloco de cobertura nula", () => {
    expect(isUnevaluated(block({ coverage: "none", delta_prob_a: null }))).toBe(
      true,
    );
  });

  it("reconhece deslocamento ausente mesmo com cobertura declarada", () => {
    // Fora do contrato, mas a tela não pode desenhar barra para `null`.
    expect(isUnevaluated(block({ coverage: "full", delta_prob_a: null }))).toBe(
      true,
    );
  });

  it("não confunde zero MEDIDO com ausência de medição", () => {
    // O par que separa os dois estados: zero medido é informação.
    expect(isUnevaluated(block({ coverage: "full", delta_prob_a: 0 }))).toBe(
      false,
    );
  });
});

describe("sortedBlocks — ordem de exibição", () => {
  it("ordena por magnitude do deslocamento, decrescente", () => {
    const avaliados = sortedBlocks(MATCHUP_BLOCK_FIXTURES)
      .filter((entry) => !isUnevaluated(entry))
      .map((entry) => entry.name);

    expect(avaliados).toEqual([
      "dimensoes-ausentes",
      "striking-profile",
      "estilo-semelhante",
      "eixos-de-estilo",
      "formato",
      "forca-de-calendario",
      "historico-qualificado",
    ]);
  });

  it("joga os blocos sem medição para o fim, na ordem da resposta", () => {
    /*
      `none` no meio do ranking leria como "influência zero" — exatamente a
      leitura proibida. A asserção falha se um `none` for ordenado como se fosse
      zero: ele viria antes de `historico-qualificado`, que tem zero MEDIDO.
    */
    const ordenados = sortedBlocks(MATCHUP_BLOCK_FIXTURES).map(
      (entry) => entry.name,
    );

    expect(ordenados.slice(-4)).toEqual([
      "dinamica-por-round",
      "divisao-categoria",
      "divisao-taxa-base",
      "confronto-de-bases",
    ]);
    expect(ordenados.indexOf("historico-qualificado")).toBeLessThan(
      ordenados.indexOf("dinamica-por-round"),
    );
  });

  it("resolve empate pela ordem da resposta", () => {
    const entrada = [
      block({ name: "primeiro", delta_prob_a: 0.02 }),
      block({ name: "segundo", delta_prob_a: -0.02 }),
    ];

    expect(sortedBlocks(entrada).map((entry) => entry.name)).toEqual([
      "primeiro",
      "segundo",
    ]);
  });

  it("não muta a lista recebida", () => {
    const entrada = [...MATCHUP_BLOCK_FIXTURES];
    sortedBlocks(entrada);

    expect(entrada.map((entry) => entry.name)).toEqual(
      MATCHUP_BLOCK_FIXTURES.map((entry) => entry.name),
    );
  });
});

describe("barScaleOf — escala da barra", () => {
  it("usa o maior deslocamento avaliado como escala", () => {
    expect(barScaleOf(MATCHUP_BLOCK_FIXTURES)).toBeCloseTo(0.062, 10);
  });

  it("devolve zero quando nenhum bloco foi avaliado", () => {
    const nenhum = MATCHUP_BLOCK_FIXTURES.filter((entry) =>
      isUnevaluated(entry),
    );

    expect(barScaleOf(nenhum)).toBe(0);
    expect(barScaleOf([])).toBe(0);
  });
});
