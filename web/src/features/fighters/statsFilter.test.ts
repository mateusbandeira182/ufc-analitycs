import { describe, expect, it } from "vitest";

import type { BoutMethod, FighterBoutOut } from "@/api/schema";
import {
  DEFAULT_STATS_FILTER,
  deriveFighterStats,
  selectBouts,
  type StatsFilter,
} from "@/features/fighters/statsFilter";
import { NULL_STRIKE_SPLITS } from "@/mocks/strikeSplits";

/*
  Filtragem e agregação client-side das estatísticas do lutador. O histórico chega
  em ordem cronológica ascendente (garantia do backend); os testes montam lutas
  com box-score controlado para travar as médias sob cada recorte (período,
  últimas N e a combinação dos dois).
*/

interface BoutOverrides {
  date: string;
  won?: boolean;
  method?: BoutMethod;
  sig?: number | null;
  td?: number | null;
  control?: number | null;
}

/** Uma luta do histórico com apenas os campos que a agregação lê. */
function makeBout(id: number, overrides: BoutOverrides): FighterBoutOut {
  return {
    bout_id: id,
    event_id: id,
    event_name: `Evento ${String(id)}`,
    event_date: overrides.date,
    method: overrides.method ?? "decision",
    round: 3,
    ending_time_seconds: 300,
    won: overrides.won ?? false,
    opponent: { fighter_id: 99, name: "Adversário" },
    stats: {
      fighter_id: 1,
      name: "Lutador",
      corner: "red",
      knockdowns: null,
      sig_strikes_landed: overrides.sig ?? null,
      sig_strikes_attempted: null,
      takedowns_landed: overrides.td ?? null,
      takedowns_attempted: null,
      submission_attempts: null,
      control_time_seconds: overrides.control ?? null,
      ...NULL_STRIKE_SPLITS,
      source: "kaggle",
    },
  };
}

// Histórico ascendente de 4 lutas com stats distintas para isolar cada recorte.
const HISTORY: FighterBoutOut[] = [
  makeBout(1, { date: "2018-01-01", sig: 10, td: 4, control: 60, won: false }),
  makeBout(2, {
    date: "2020-01-01",
    sig: 20,
    td: 2,
    control: 120,
    won: true,
    method: "ko_tko",
  }),
  makeBout(3, {
    date: "2022-01-01",
    sig: 30,
    td: 0,
    control: 180,
    won: true,
    method: "submission",
  }),
  makeBout(4, {
    date: "2024-01-01",
    sig: 40,
    td: 0,
    control: 240,
    won: true,
    method: "ko_tko",
  }),
];

describe("selectBouts", () => {
  it("mantém todas as lutas com o filtro padrão", () => {
    expect(selectBouts(HISTORY, DEFAULT_STATS_FILTER)).toHaveLength(4);
  });

  it("recorta as últimas N pela cauda (mais recentes)", () => {
    const last2 = selectBouts(HISTORY, { ...DEFAULT_STATS_FILTER, lastN: 2 });
    expect(last2.map((bout) => bout.bout_id)).toEqual([3, 4]);
  });

  it("filtra pelo período (limites inclusivos)", () => {
    const filter: StatsFilter = {
      lastN: null,
      dateFrom: "2020-01-01",
      dateTo: "2022-12-31",
    };
    expect(selectBouts(HISTORY, filter).map((bout) => bout.bout_id)).toEqual([
      2, 3,
    ]);
  });

  it("combina período e últimas N por interseção (as últimas N dentro do período)", () => {
    const filter: StatsFilter = {
      lastN: 2,
      dateFrom: "2018-01-01",
      dateTo: "2022-12-31",
    };
    // Período deixa {1,2,3}; as últimas 2 desse subconjunto são {2,3}.
    expect(selectBouts(HISTORY, filter).map((bout) => bout.bout_id)).toEqual([
      2, 3,
    ]);
  });

  it("pedir mais lutas do que existem devolve todas as disponíveis", () => {
    expect(
      selectBouts(HISTORY, { ...DEFAULT_STATS_FILTER, lastN: 8 }),
    ).toHaveLength(4);
  });
});

describe("deriveFighterStats", () => {
  it("recompõe médias e como venceu sobre todo o histórico", () => {
    const stats = deriveFighterStats(HISTORY, DEFAULT_STATS_FILTER);

    expect(stats.bouts_counted).toBe(4);
    expect(stats.avg_sig_strikes_landed).toBe(25); // (10+20+30+40)/4
    expect(stats.avg_takedowns_landed).toBe(1.5); // (4+2+0+0)/4
    expect(stats.avg_control_time_seconds).toBe(150); // (60+120+180+240)/4
    expect(stats.wins_by_method).toEqual({ ko_tko: 2, submission: 1 });
  });

  it("reflete o recorte de últimas N nas médias e nas vitórias", () => {
    const stats = deriveFighterStats(HISTORY, {
      ...DEFAULT_STATS_FILTER,
      lastN: 2,
    });

    expect(stats.bouts_counted).toBe(2);
    expect(stats.avg_sig_strikes_landed).toBe(35); // (30+40)/2
    expect(stats.wins_by_method).toEqual({ ko_tko: 1, submission: 1 });
  });

  it("ignora nulos na média, dividindo só pelos valores presentes", () => {
    const bouts = [
      makeBout(1, { date: "2020-01-01", sig: 10 }),
      makeBout(2, { date: "2021-01-01", sig: null }),
      makeBout(3, { date: "2022-01-01", sig: 30 }),
    ];
    const stats = deriveFighterStats(bouts, DEFAULT_STATS_FILTER);

    expect(stats.bouts_counted).toBe(3); // conta a luta mesmo com stat nula
    expect(stats.avg_sig_strikes_landed).toBe(20); // (10+30)/2, não /3
  });

  it("média é nula quando nenhum valor está presente no recorte", () => {
    const bouts = [makeBout(1, { date: "2020-01-01", sig: null, td: null })];
    const stats = deriveFighterStats(bouts, DEFAULT_STATS_FILTER);

    expect(stats.avg_sig_strikes_landed).toBeNull();
    expect(stats.avg_takedowns_landed).toBeNull();
  });

  it("recorte vazio zera a contagem e não acusa vitórias", () => {
    const stats = deriveFighterStats(HISTORY, {
      lastN: null,
      dateFrom: "2030-01-01",
      dateTo: null,
    });

    expect(stats.bouts_counted).toBe(0);
    expect(stats.avg_sig_strikes_landed).toBeNull();
    expect(stats.wins_by_method).toEqual({});
  });
});
