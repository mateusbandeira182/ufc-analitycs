import { describe, expect, it } from "vitest";

import {
  formatEndingTime,
  formatHeight,
  formatIsoDate,
  formatMethod,
  formatReach,
  formatResult,
  formatWeight,
} from "@/features/fighters/format";

describe("formatMethod", () => {
  it("traduz cada método do enum BoutMethod para pt-BR", () => {
    expect(formatMethod("ko_tko")).toBe("KO/TKO");
    expect(formatMethod("submission")).toBe("Finalização");
    expect(formatMethod("decision")).toBe("Decisão");
    expect(formatMethod("dq")).toBe("Desqualificação");
    expect(formatMethod("no_contest")).toBe("Sem resultado");
  });
});

describe("formatResult", () => {
  it("retorna Vitória quando o lutador venceu", () => {
    expect(formatResult({ won: true, method: "ko_tko" })).toBe("Vitória");
  });

  it("retorna Sem resultado quando a luta foi no contest", () => {
    expect(formatResult({ won: false, method: "no_contest" })).toBe(
      "Sem resultado",
    );
  });

  it("retorna Derrota quando não venceu e houve resultado", () => {
    expect(formatResult({ won: false, method: "decision" })).toBe("Derrota");
  });
});

describe("formatEndingTime", () => {
  it("formata round e tempo como 'R{round} {m:ss}'", () => {
    expect(formatEndingTime(2, 255)).toBe("R2 4:15");
  });

  it("preenche o segundo com zero à esquerda", () => {
    expect(formatEndingTime(1, 65)).toBe("R1 1:05");
  });

  it("retorna traço quando round ou tempo são nulos", () => {
    expect(formatEndingTime(null, null)).toBe("—");
    expect(formatEndingTime(2, null)).toBe("—");
    expect(formatEndingTime(null, 100)).toBe("—");
  });
});

describe("formatIsoDate", () => {
  it("formata a data ISO em pt-BR sem deslize de fuso", () => {
    // 2016-07-09 deve permanecer 09/07/2016 mesmo em fusos negativos.
    expect(formatIsoDate("2016-07-09")).toBe("09/07/2016");
  });

  it("retorna traço quando a data é nula", () => {
    expect(formatIsoDate(null)).toBe("—");
  });
});

describe("formatHeight", () => {
  it("formata a altura em centímetros e traço quando nula", () => {
    expect(formatHeight(193)).toBe("193 cm");
    expect(formatHeight(null)).toBe("—");
  });
});

describe("formatReach", () => {
  it("formata o alcance em centímetros e traço quando nulo", () => {
    expect(formatReach(215)).toBe("215 cm");
    expect(formatReach(null)).toBe("—");
  });
});

describe("formatWeight", () => {
  it("formata o peso inteiro em quilos sem casa decimal", () => {
    expect(formatWeight(93)).toBe("93 kg");
  });

  it("formata o peso fracionário com uma casa em pt-BR (vírgula)", () => {
    expect(formatWeight(70.3)).toBe("70,3 kg");
  });

  it("retorna traço quando o peso é nulo", () => {
    expect(formatWeight(null)).toBe("—");
  });
});
