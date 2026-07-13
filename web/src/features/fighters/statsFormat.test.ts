import { describe, expect, it } from "vitest";

import {
  finishSegments,
  formatAvgControlTime,
  formatAverage,
} from "@/features/fighters/statsFormat";
import { DASH } from "@/lib/format";

/*
  Funções puras de apresentação das estatísticas agregadas do lutador. As médias
  vêm do backend como número (float) ou nulo; a formatação (uma casa decimal em
  pt-BR, tempo de controle como m:ss) é apresentação feita aqui no cliente.
*/

describe("formatAverage", () => {
  it("formata a média com uma casa decimal em pt-BR (vírgula)", () => {
    expect(formatAverage(62.53)).toBe("62,5");
    expect(formatAverage(1.3)).toBe("1,3");
  });

  it("preserva o zero com uma casa decimal", () => {
    expect(formatAverage(0)).toBe("0,0");
  });

  it("retorna traço quando a média é nula", () => {
    expect(formatAverage(null)).toBe(DASH);
  });
});

describe("formatAvgControlTime", () => {
  it("formata a média de segundos como m:ss, arredondando", () => {
    expect(formatAvgControlTime(204)).toBe("3:24");
    expect(formatAvgControlTime(204.4)).toBe("3:24");
  });

  it("preenche o segundo com zero à esquerda", () => {
    expect(formatAvgControlTime(65)).toBe("1:05");
  });

  it("retorna traço quando a média é nula", () => {
    expect(formatAvgControlTime(null)).toBe(DASH);
  });
});

describe("finishSegments", () => {
  it("gera segmentos em ordem canônica, só com contagem positiva", () => {
    const segments = finishSegments({
      decision: 11,
      ko_tko: 10,
      submission: 6,
    });

    expect(segments).toEqual([
      { method: "ko_tko", label: "KO/TKO", count: 10 },
      { method: "submission", label: "Finalização", count: 6 },
      { method: "decision", label: "Decisão", count: 11 },
    ]);
  });

  it("omite métodos ausentes ou zerados", () => {
    expect(finishSegments({ ko_tko: 3, decision: 0 })).toEqual([
      { method: "ko_tko", label: "KO/TKO", count: 3 },
    ]);
  });

  it("retorna lista vazia quando não há vitórias", () => {
    expect(finishSegments({})).toEqual([]);
  });
});
