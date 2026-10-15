import { describe, expect, it } from "vitest";

import {
  clampProbability,
  formatAttributeValue,
  formatProbability,
} from "@/features/confronto/format";
import { DASH } from "@/lib/format";

describe("formatProbability", () => {
  it("formata a fração como percentual inteiro", () => {
    expect(formatProbability(0.51)).toBe("51%");
    expect(formatProbability(0.5094)).toBe("51%");
    expect(formatProbability(0)).toBe("0%");
    expect(formatProbability(1)).toBe("100%");
  });

  it("saneia valores fora de [0,1] e não finitos (nunca NaN/Infinity)", () => {
    expect(formatProbability(1.5)).toBe("100%");
    expect(formatProbability(-0.2)).toBe("0%");
    expect(formatProbability(Number.NaN)).toBe("0%");
    expect(formatProbability(Number.POSITIVE_INFINITY)).toBe("0%");
  });
});

describe("clampProbability", () => {
  it("mantém frações válidas e limita as inválidas ao intervalo [0,1]", () => {
    expect(clampProbability(0.42)).toBe(0.42);
    expect(clampProbability(2)).toBe(1);
    expect(clampProbability(-1)).toBe(0);
    expect(clampProbability(Number.NaN)).toBe(0);
  });
});

describe("formatAttributeValue", () => {
  it("formata o atributo as-of cru com duas casas em pt-BR", () => {
    expect(formatAttributeValue(0.675)).toBe("0,68");
    expect(formatAttributeValue(4.312)).toBe("4,31");
    expect(formatAttributeValue(214.5)).toBe("214,50");
  });

  it("preserva o zero MEDIDO, que é taxa legítima", () => {
    // Quem nunca finalizou tem `finish_rate_prior` zero de verdade.
    expect(formatAttributeValue(0)).toBe("0,00");
  });

  it("devolve traço para ausência de valor as-of", () => {
    // `null` é ausência de medição; `0` seria uma taxa afirmada sem medição.
    expect(formatAttributeValue(null)).toBe(DASH);
  });

  it("não colapsa duas casas em uma", () => {
    /*
      Duas casas, e não uma: as oito chaves misturam taxa em [0,1], taxa por
      minuto e segundos. Uma casa só colapsaria 0,675 em 0,7.
    */
    expect(formatAttributeValue(0.675)).not.toBe("0,7");
  });
});
