import { describe, expect, it } from "vitest";

import { clampProbability, formatProbability } from "@/features/matchup/format";

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
