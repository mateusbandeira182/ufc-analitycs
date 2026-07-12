import { describe, expect, it } from "vitest";

import type { BoutDetailOut, BoutFighterStatsOut } from "@/api/schema";
import {
  formatAttempts,
  formatDuration,
  formatStat,
  resolveResult,
} from "@/features/bouts/format";
import { DASH } from "@/lib/format";

/*
  Funções puras de apresentação da luta. A duração (tempo de encerramento e tempo
  de controle) é segundo inteiro no contrato (decisão de schema); a formatação
  mm:ss é apresentação, feita aqui no cliente.
*/

function stats(
  overrides: Partial<BoutFighterStatsOut> = {},
): BoutFighterStatsOut {
  return {
    fighter_id: 1,
    name: "Jon Jones",
    corner: "red",
    knockdowns: 0,
    sig_strikes_landed: 45,
    sig_strikes_attempted: 80,
    takedowns_landed: 2,
    takedowns_attempted: 5,
    submission_attempts: 1,
    control_time_seconds: 315,
    source: "kaggle",
    ...overrides,
  };
}

function bout(overrides: Partial<BoutDetailOut> = {}): BoutDetailOut {
  return {
    id: 1,
    event: {
      id: 10,
      name: "UFC 300",
      date: "2024-04-13",
      location: "Las Vegas, USA",
      source: "kaggle",
    },
    winner_id: 1,
    method: "decision",
    round: 3,
    ending_time_seconds: 300,
    weight_class: "Light Heavyweight",
    source: "kaggle",
    fighters: [
      stats({ fighter_id: 1, name: "Jon Jones", corner: "red" }),
      stats({ fighter_id: 2, name: "Daniel Cormier", corner: "blue" }),
    ],
    ...overrides,
  };
}

describe("formatDuration", () => {
  it("formata segundos como mm:ss", () => {
    expect(formatDuration(315)).toBe("5:15");
  });

  it("preenche o segundo com zero à esquerda", () => {
    expect(formatDuration(65)).toBe("1:05");
  });

  it("formata zero como 0:00", () => {
    expect(formatDuration(0)).toBe("0:00");
  });

  it("retorna traço quando a duração é nula", () => {
    expect(formatDuration(null)).toBe(DASH);
  });
});

describe("formatStat", () => {
  it("mostra o número quando presente (inclusive zero)", () => {
    expect(formatStat(0)).toBe("0");
    expect(formatStat(3)).toBe("3");
  });

  it("retorna traço quando o valor é nulo", () => {
    expect(formatStat(null)).toBe(DASH);
  });
});

describe("formatAttempts", () => {
  it("combina acertados e tentados como 'x de y'", () => {
    expect(formatAttempts(45, 80)).toBe("45 de 80");
  });

  it("usa traço no campo nulo, preservando o outro", () => {
    expect(formatAttempts(null, 80)).toBe(`${DASH} de 80`);
    expect(formatAttempts(2, null)).toBe(`2 de ${DASH}`);
  });

  it("retorna um único traço quando ambos são nulos", () => {
    expect(formatAttempts(null, null)).toBe(DASH);
  });
});

describe("resolveResult", () => {
  it("resolve o vencedor pelo nome cruzando winner_id com o canto", () => {
    expect(resolveResult(bout({ winner_id: 2 }))).toEqual({
      kind: "winner",
      name: "Daniel Cormier",
    });
  });

  it("classifica como empate quando winner_id é nulo e o método não é no contest", () => {
    expect(
      resolveResult(bout({ winner_id: null, method: "decision" })),
    ).toEqual({ kind: "draw" });
  });

  it("classifica como sem resultado quando o método é no contest", () => {
    expect(
      resolveResult(bout({ winner_id: null, method: "no_contest" })),
    ).toEqual({ kind: "no_contest" });
  });
});
