import { describe, expect, it } from "vitest";

import type { BoutCardOut } from "@/api/schema";
import { groupByCardSection } from "@/features/events/cardOrder";

/**
 * Luta de card mínima para exercitar o agrupamento. Só `id`, `card_section` e
 * `bout_order` importam aqui; o resto preenche o contrato. `bout_order` entra
 * com o valor real da Cito (1001 na principal) de propósito: a função não pode
 * assumir faixa densa nem sequencial.
 */
function makeBout(
  id: number,
  cardSection: string | null,
  boutOrder: number | null,
): BoutCardOut {
  return {
    id,
    winner_id: null,
    method: null,
    round: null,
    ending_time_seconds: null,
    weight_class: null,
    source: "cito",
    status: "scheduled",
    card_section: cardSection,
    bout_order: boutOrder,
    fighters: [],
  };
}

describe("groupByCardSection", () => {
  it("agrupa por `card_section` do payload com a principal em bout_order 1001", () => {
    const bouts = [
      makeBout(1, "Main Card", 1001),
      makeBout(2, "Main Card", 1002),
      makeBout(3, "Prelims", 2001),
    ];

    const groups = groupByCardSection(bouts);

    expect(groups).toHaveLength(2);
    expect(groups[0]?.section).toBe("Main Card");
    expect(groups[0]?.bouts.map((b) => b.id)).toEqual([1, 2]);
    expect(groups[1]?.section).toBe("Prelims");
    expect(groups[1]?.bouts.map((b) => b.id)).toEqual([3]);
  });

  it("forma um grupo próprio quando `card_section` é nulo", () => {
    const bouts = [makeBout(1, null, null), makeBout(2, null, null)];

    const groups = groupByCardSection(bouts);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.section).toBeNull();
    expect(groups[0]?.bouts.map((b) => b.id)).toEqual([1, 2]);
  });

  it("ordena os grupos pela primeira aparição da seção, não pelo bout_order", () => {
    // "Prelims" aparece primeiro no array mesmo com `bout_order` maior: a ordem
    // entre grupos é a de primeira aparição, nunca derivada do `bout_order`.
    const bouts = [
      makeBout(3, "Prelims", 2001),
      makeBout(1, "Main Card", 1001),
      makeBout(4, "Prelims", 2002),
    ];

    const groups = groupByCardSection(bouts);

    expect(groups.map((g) => g.section)).toEqual(["Prelims", "Main Card"]);
    expect(groups[0]?.bouts.map((b) => b.id)).toEqual([3, 4]);
  });

  it("preserva a ordem de entrada dentro de cada grupo, sem reordenar por bout_order", () => {
    // Entrada fora de ordem de `bout_order`: o servidor é a autoridade da ordem
    // (`bout_order asc nulls_last, id asc`) e a função não a recalcula.
    const bouts = [
      makeBout(2, "Main Card", 1005),
      makeBout(1, "Main Card", 1001),
    ];

    const groups = groupByCardSection(bouts);

    expect(groups[0]?.bouts.map((b) => b.id)).toEqual([2, 1]);
  });

  it("achatar a saída reproduz exatamente a entrada na ordem que o servidor entrega", () => {
    // O backend ordena por `bout_order asc nulls_last, id asc`, o que deixa cada
    // seção contígua e joga as lutas sem contexto para o fim. Nessa entrada --
    // a real -- achatar a saída devolve o próprio array, na mesma ordem.
    const bouts = [
      makeBout(1, "Main Card", 1001),
      makeBout(2, "Main Card", 1002),
      makeBout(3, "Prelims", 2001),
      makeBout(4, null, null),
      makeBout(5, null, null),
    ];

    const achatado = groupByCardSection(bouts).flatMap((g) => g.bouts);

    expect(achatado).toEqual(bouts);
  });

  it("nunca perde nem duplica luta, mesmo com as seções intercaladas", () => {
    // Invariante que impede uma luta de desaparecer em silêncio. Com seções
    // intercaladas o agrupamento necessariamente as reagrupa, mas o conjunto e a
    // contagem são preservados.
    const bouts = [
      makeBout(1, "Main Card", 1001),
      makeBout(2, null, null),
      makeBout(3, "Prelims", 2001),
      makeBout(4, "Main Card", 1002),
      makeBout(5, null, null),
    ];

    const achatado = groupByCardSection(bouts).flatMap((g) => g.bouts);

    expect(achatado).toHaveLength(bouts.length);
    expect(new Set(achatado)).toEqual(new Set(bouts));
    // Primeira aparição: "Main Card" (1), `null` (2), "Prelims" (3).
    expect(achatado.map((b) => b.id)).toEqual([1, 4, 2, 5, 3]);
  });

  it("devolve lista vazia para card vazio", () => {
    expect(groupByCardSection([])).toEqual([]);
  });
});
