import { useState } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { FighterFinishBreakdown } from "@/features/fighters/FighterFinishBreakdown";
import { FighterStatsFilters } from "@/features/fighters/FighterStatsFilters";
import {
  DEFAULT_STATS_FILTER,
  deriveFighterStats,
  type AggregatedStats,
  type StatsFilter,
} from "@/features/fighters/statsFilter";
import {
  formatAvgControlTime,
  formatAverage,
} from "@/features/fighters/statsFormat";
import { useFighterBouts } from "@/features/fighters/useFighterBouts";

/*
  Bloco do RECORTE: as médias recompostas no cliente sobre um corte do histórico
  granular (últimas N lutas e/ou período). O filtro foi preservado por decisão do
  humano (D-13), contra a recomendação técnica, e o preço acordado foi este: o
  bloco **se identifica na tela**, para que os números dele nunca sejam lidos como
  os de CARREIRA nem os de FORMA ATUAL.

  O isolamento é **estrutural, não vigilância**: o `StatsFilter` vive aqui dentro
  e nenhuma outra seção o recebe, então não existe caminho pelo qual o seletor
  alcance os blocos que leem o servidor. Elevar este estado para `FighterPage`
  recriaria exatamente o acoplamento que a D-13 comprou o rótulo para evitar.
*/

const HEADING_ID = "fighter-stats-excerpt";

/**
 * Seção do recorte na página do lutador. Consome o histórico granular via
 * `useFighterBouts` e trata os três estados do server-state, mais o lutador sem
 * lutas e o recorte que não pega nenhuma luta — nos dois últimos os filtros
 * seguem visíveis, para o usuário ajustar.
 */
export function FighterStatsExcerpt({ fighterId }: { fighterId: number }) {
  const { data, isPending, isError } = useFighterBouts(fighterId);
  const [filter, setFilter] = useState<StatsFilter>(DEFAULT_STATS_FILTER);

  return (
    <section aria-labelledby={HEADING_ID}>
      <h2
        id={HEADING_ID}
        className="font-display text-xl font-bold uppercase tracking-wide"
      >
        {excerptTitle(filter)}
      </h2>
      <p className="mb-5 mt-1 text-[0.7rem] uppercase tracking-widest text-muted-foreground">
        Médias recompostas no navegador, sobre o corte que você escolher
      </p>

      {isPending ? (
        <div
          aria-busy="true"
          aria-label="Carregando recorte"
          className="grid gap-4"
        >
          <Skeleton className="h-40 w-full" />
        </div>
      ) : isError ? (
        <p role="status" className="text-sm text-muted-foreground">
          Não foi possível recompor o recorte deste lutador.
        </p>
      ) : data.length === 0 ? (
        <p
          role="status"
          className="rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center text-sm text-muted-foreground"
        >
          Nenhuma luta registrada no acervo para recompor o recorte.
        </p>
      ) : (
        <div className="grid gap-8">
          <FighterStatsFilters filter={filter} onChange={setFilter} />
          <ExcerptStats stats={deriveFighterStats(data, filter)} />
        </div>
      )}
    </section>
  );
}

/**
 * Título que identifica o corte vigente. É o rótulo que a D-13 cobrou em troca
 * de manter o filtro: sem ele, "25,0" na tela não diz sobre quais lutas é.
 */
function excerptTitle(filter: StatsFilter): string {
  return filter.lastN === null
    ? "Recorte: todas as lutas"
    : `Recorte: últimas ${String(filter.lastN)} lutas`;
}

/**
 * Os números do recorte: as três médias e o "como venceu" filtrado. O painel é
 * tracejado de propósito — na linguagem desta interface, borda tracejada é o que
 * o leitor escolheu olhar, borda cheia é o que o acervo afirma.
 */
function ExcerptStats({ stats }: { stats: AggregatedStats }) {
  if (stats.bouts_counted === 0) {
    return (
      <p
        role="status"
        className="rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center text-sm text-muted-foreground"
      >
        Nenhuma luta corresponde ao recorte selecionado. Ajuste o período ou o
        número de lutas.
      </p>
    );
  }

  const metrics = [
    {
      label: "Golpes significativos",
      unit: "por luta",
      value: formatAverage(stats.avg_sig_strikes_landed),
    },
    {
      label: "Quedas",
      unit: "por luta",
      value: formatAverage(stats.avg_takedowns_landed),
    },
    {
      label: "Tempo de controle",
      unit: "por luta",
      value: formatAvgControlTime(stats.avg_control_time_seconds),
    },
  ];

  return (
    <div className="grid gap-6 rounded-lg border border-dashed border-border bg-card/50 p-6">
      <dl className="grid gap-6 sm:grid-cols-3">
        {metrics.map((metric) => (
          <div key={metric.label}>
            <dd className="font-mono text-3xl font-bold leading-none tabular-nums text-belt-gold">
              {metric.value}
            </dd>
            <dt className="mt-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {metric.label}
              <span className="ml-1 text-muted-foreground/60">
                {metric.unit}
              </span>
            </dt>
          </div>
        ))}
      </dl>

      <p className="font-mono text-[0.7rem] uppercase tracking-widest tabular-nums text-muted-foreground">
        {stats.bouts_counted} lutas contabilizadas
      </p>

      <FighterFinishBreakdown
        title="Como venceu no recorte"
        headingLevel={3}
        winsByMethod={stats.wins_by_method}
      />
    </div>
  );
}
