import { describe, expect, it } from "vitest";

import { isAwaitingSync, todayIsoDate } from "@/features/events/syncStatus";

// `today` é sempre injetado: teste que lê o relógio apodrece, e a comparação de
// string `YYYY-MM-DD` evita o deslize de fuso de `new Date(iso)`.
const HOJE = "2026-10-15";

describe("isAwaitingSync", () => {
  it("é verdadeiro quando a data do evento já passou", () => {
    expect(isAwaitingSync("2026-09-05", HOJE)).toBe(true);
    expect(isAwaitingSync("2026-10-14", HOJE)).toBe(true);
  });

  it("é falso no próprio dia do evento", () => {
    // Evento de hoje ainda não aconteceu: não há atraso de sincronização.
    expect(isAwaitingSync(HOJE, HOJE)).toBe(false);
  });

  it("é falso quando a data do evento é futura", () => {
    expect(isAwaitingSync("2026-10-16", HOJE)).toBe(false);
    expect(isAwaitingSync("2026-12-12", HOJE)).toBe(false);
  });

  it("compara por string, sem construir Date (nenhum deslize de fuso)", () => {
    // Em fuso negativo, `new Date('2026-10-15')` cai em 14/10 local e inverteria
    // o resultado da comparação de igualdade.
    expect(isAwaitingSync("2026-01-01", "2026-01-01")).toBe(false);
    expect(isAwaitingSync("2025-12-31", "2026-01-01")).toBe(true);
  });
});

describe("todayIsoDate", () => {
  it("formata a data como `YYYY-MM-DD` com mês e dia preenchidos à esquerda", () => {
    // Construído pelos componentes LOCAIS da data: `toISOString()` converteria
    // para UTC e devolveria o dia anterior num fuso negativo, que é exatamente o
    // deslize que `isAwaitingSync` existe para não cometer.
    expect(todayIsoDate(new Date(2026, 0, 5))).toBe("2026-01-05");
    expect(todayIsoDate(new Date(2026, 11, 31))).toBe("2026-12-31");
  });

  it("usa o relógio do sistema quando nenhuma data é passada", () => {
    expect(todayIsoDate()).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
});
