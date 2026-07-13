import type { FighterBoutOut } from "@/api/schema";

/*
  Filtragem e agregação das estatísticas do lutador a partir do histórico granular
  (`/fighters/:id/bouts`). O backend expõe as médias sobre TODAS as lutas; aqui o
  cliente recorta esse mesmo box-score por-luta — por período e/ou pelas últimas N
  lutas — e recompõe as médias on demand. Nada é pré-agregado: preserva a série
  temporal (CLAUDE.md), só que sob o recorte escolhido na SPA.
*/

/** Opções de "últimas N lutas" oferecidas na UI (o "Todas" é o `null` do filtro). */
export const LAST_N_OPTIONS = [2, 3, 4, 5, 6, 7, 8] as const;

/** Quantidade de lutas mais recentes a considerar; `null` significa "todas". */
export type LastN = (typeof LAST_N_OPTIONS)[number] | null;

/**
 * Recorte aplicado ao histórico antes de agregar. Os dois eixos combinam por
 * interseção: o período estreita o conjunto e, do que sobra, ficam as últimas N.
 * Datas em ISO `YYYY-MM-DD` (comparáveis como string); `null` = limite aberto.
 */
export interface StatsFilter {
  lastN: LastN;
  dateFrom: string | null;
  dateTo: string | null;
}

/** Filtro inicial: todas as lutas, sem recorte de período (equivale ao acervo inteiro). */
export const DEFAULT_STATS_FILTER: StatsFilter = {
  lastN: null,
  dateFrom: null,
  dateTo: null,
};

/**
 * Médias recompostas sob o recorte, no mesmo formato do agregado do backend.
 * Médias `null` quando não há valor não-nulo a agregar (nenhuma luta, ou o stat
 * é sempre nulo no recorte). `wins_by_method` só conta lutas vencidas.
 */
export interface AggregatedStats {
  bouts_counted: number;
  avg_sig_strikes_landed: number | null;
  avg_takedowns_landed: number | null;
  avg_control_time_seconds: number | null;
  wins_by_method: Record<string, number>;
}

/**
 * Média que ignora nulos, espelhando `func.avg` do Postgres: divide pela
 * quantidade de valores presentes e devolve `null` quando não há nenhum.
 */
function average(values: (number | null)[]): number | null {
  const present = values.filter((value): value is number => value !== null);
  if (present.length === 0) {
    return null;
  }
  return present.reduce((sum, value) => sum + value, 0) / present.length;
}

/**
 * Aplica o recorte ao histórico (assumido em ordem cronológica ascendente, como o
 * backend entrega): filtra por período e então mantém as últimas N pela cauda.
 * A ordem período-antes-de-N garante "as últimas N lutas dentro do período".
 */
export function selectBouts(
  bouts: FighterBoutOut[],
  filter: StatsFilter,
): FighterBoutOut[] {
  let selected = bouts;
  if (filter.dateFrom !== null) {
    const from = filter.dateFrom;
    selected = selected.filter((bout) => bout.event_date >= from);
  }
  if (filter.dateTo !== null) {
    const to = filter.dateTo;
    selected = selected.filter((bout) => bout.event_date <= to);
  }
  if (filter.lastN !== null) {
    selected = selected.slice(-filter.lastN);
  }
  return selected;
}

/**
 * Recompõe as médias e o "como venceu" sobre o recorte do histórico. `bouts_counted`
 * é o número de lutas do recorte; cada média ignora as lutas cujo stat é nulo (o
 * denominador é só o que existe), coerente com o agregado do backend.
 */
export function deriveFighterStats(
  bouts: FighterBoutOut[],
  filter: StatsFilter,
): AggregatedStats {
  const selected = selectBouts(bouts, filter);

  const winsByMethod: Record<string, number> = {};
  for (const bout of selected) {
    if (bout.won) {
      winsByMethod[bout.method] = (winsByMethod[bout.method] ?? 0) + 1;
    }
  }

  return {
    bouts_counted: selected.length,
    avg_sig_strikes_landed: average(
      selected.map((bout) => bout.stats.sig_strikes_landed),
    ),
    avg_takedowns_landed: average(
      selected.map((bout) => bout.stats.takedowns_landed),
    ),
    avg_control_time_seconds: average(
      selected.map((bout) => bout.stats.control_time_seconds),
    ),
    wins_by_method: winsByMethod,
  };
}
