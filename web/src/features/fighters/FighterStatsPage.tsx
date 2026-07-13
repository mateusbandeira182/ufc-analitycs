import type { ReactNode } from "react";
import { useState } from "react";

import { AlertTriangle, UserX } from "lucide-react";
import { Link, useParams } from "react-router";

import { ApiError } from "@/api/client";
import { Skeleton } from "@/components/ui/skeleton";
import { FighterStatsFilters } from "@/features/fighters/FighterStatsFilters";
import { FighterStrikingProfile } from "@/features/fighters/FighterStrikingProfile";
import {
  DEFAULT_STATS_FILTER,
  deriveFighterStats,
  type AggregatedStats,
  type StatsFilter,
} from "@/features/fighters/statsFilter";
import {
  finishSegments,
  formatAvgControlTime,
  formatAverage,
} from "@/features/fighters/statsFormat";
import { useFighter } from "@/features/fighters/useFighter";
import { useFighterBouts } from "@/features/fighters/useFighterBouts";

/**
 * Rota `/fighters/:id/stats`: as estatísticas agregadas do atleta. Lê o id da URL,
 * usa `useFighter` para o cabeçalho (nome + volta ao detalhe) e delega as médias
 * ao `FighterStatsBody`. Trata carregamento, erro genérico e lutador inexistente
 * (404), coerente com a página do lutador.
 */
export function FighterStatsPage() {
  const { id } = useParams();
  const fighterId = Number(id);
  const { data, isPending, isError, error } = useFighter(fighterId);

  if (Number.isNaN(fighterId)) {
    return <NotFoundScreen />;
  }

  if (isPending) {
    return <FighterStatsPageSkeleton />;
  }

  if (isError) {
    const notFound = error instanceof ApiError && error.status === 404;
    return notFound ? (
      <NotFoundScreen />
    ) : (
      <StatusScreen
        icon={<AlertTriangle className="size-6 text-primary" />}
        title="Não foi possível carregar o lutador"
        description="A API não respondeu como esperado. Tente novamente em instantes."
      />
    );
  }

  return (
    <section className="mx-auto w-full max-w-5xl px-4 py-10">
      <header className="mb-8">
        <p className="font-display text-xs font-medium uppercase tracking-[0.3em] text-primary">
          Estatísticas
        </p>
        <h1 className="mt-1 font-display text-4xl font-bold uppercase tracking-tight">
          <Link
            to={`/fighters/${String(fighterId)}`}
            className="rounded-sm hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {data.name}
          </Link>
        </h1>
      </header>

      <div className="grid gap-12">
        <FighterStatsBody fighterId={fighterId} />
        <FighterStrikingProfile fighterId={fighterId} />
      </div>
    </section>
  );
}

/**
 * Corpo das estatísticas: consome o histórico granular via `useFighterBouts` e
 * recompõe as médias client-side sob o recorte escolhido (últimas N lutas e/ou
 * período). Trata carregamento (skeleton), erro (mensagem legível), lutador sem
 * lutas no acervo e sucesso (filtros + painel em pé/no chão + como venceu). Um
 * recorte que não pega nenhuma luta mantém os filtros visíveis com uma nota.
 */
function FighterStatsBody({ fighterId }: { fighterId: number }) {
  const { data, isPending, isError } = useFighterBouts(fighterId);
  const [filter, setFilter] = useState<StatsFilter>(DEFAULT_STATS_FILTER);

  if (isPending) {
    return (
      <div
        aria-busy="true"
        aria-label="Carregando estatísticas"
        className="grid gap-4"
      >
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Não foi possível carregar as estatísticas deste lutador.
      </p>
    );
  }

  if (data.length === 0) {
    return (
      <p
        role="status"
        className="rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center text-sm text-muted-foreground"
      >
        Ainda não há estatísticas agregadas para este lutador — nenhuma luta
        registrada no acervo.
      </p>
    );
  }

  const stats = deriveFighterStats(data, filter);

  return (
    <div className="grid gap-8">
      <FighterStatsFilters filter={filter} onChange={setFilter} />
      {stats.bouts_counted === 0 ? (
        <p
          role="status"
          className="rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center text-sm text-muted-foreground"
        >
          Nenhuma luta corresponde ao recorte selecionado. Ajuste o período ou o
          número de lutas.
        </p>
      ) : (
        <FighterStats stats={stats} />
      )}
    </div>
  );
}

/**
 * Assinatura visual da página: o cartel do atleta partido pela linha central do
 * octógono. À esquerda o jogo em pé (canto vermelho); à direita o jogo no chão
 * (canto azul). Abaixo, a barra "Como venceu" traduz `wins_by_method`.
 */
function FighterStats({ stats }: { stats: AggregatedStats }) {
  return (
    <div className="grid gap-8">
      <p className="font-mono text-[0.7rem] uppercase tracking-widest tabular-nums text-muted-foreground">
        Médias por luta · {stats.bouts_counted} lutas contabilizadas
      </p>

      <div className="relative grid gap-px overflow-hidden rounded-lg border border-border bg-border sm:grid-cols-2">
        {/* Linha central do octógono: divide o em pé do no chão. */}
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-6 left-1/2 hidden w-px -translate-x-1/2 bg-gradient-to-b from-transparent via-belt-gold/40 to-transparent sm:block"
        />
        <Discipline
          corner="red"
          eyebrow="Em pé"
          caption="Trocação"
          metrics={[
            {
              label: "Golpes significativos",
              value: formatAverage(stats.avg_sig_strikes_landed),
            },
          ]}
        />
        <Discipline
          corner="blue"
          eyebrow="No chão"
          caption="Wrestling e controle"
          metrics={[
            {
              label: "Quedas",
              value: formatAverage(stats.avg_takedowns_landed),
            },
            {
              label: "Tempo de controle",
              value: formatAvgControlTime(stats.avg_control_time_seconds),
            },
          ]}
        />
      </div>

      <FinishBreakdown winsByMethod={stats.wins_by_method} />
    </div>
  );
}

interface DisciplineMetric {
  label: string;
  value: string;
}

/**
 * Um lado do octógono: o rótulo do jogo (em pé / no chão) com a faixa do canto e
 * uma ou mais médias. O canto é sinalizado por texto e por faixa — nunca só por cor.
 */
function Discipline({
  corner,
  eyebrow,
  caption,
  metrics,
}: {
  corner: "red" | "blue";
  eyebrow: string;
  caption: string;
  metrics: DisciplineMetric[];
}) {
  const accent = corner === "red" ? "bg-corner-red" : "bg-corner-blue";

  return (
    <section className="relative bg-card p-6 pt-7 sm:p-8 sm:pt-9">
      <span
        aria-hidden="true"
        className={`absolute inset-x-0 top-0 h-1 ${accent}`}
      />
      <p className="font-display text-lg font-bold uppercase tracking-[0.2em]">
        {eyebrow}
      </p>
      <p className="mt-0.5 text-[0.65rem] uppercase tracking-widest text-muted-foreground">
        {caption}
      </p>
      <dl className="mt-6 grid gap-5">
        {metrics.map((metric) => (
          <div key={metric.label}>
            <dd className="font-mono text-5xl font-bold leading-none tabular-nums text-belt-gold">
              {metric.value}
            </dd>
            <dt className="mt-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {metric.label}
              <span className="ml-1 text-muted-foreground/60">por luta</span>
            </dt>
          </div>
        ))}
      </dl>
    </section>
  );
}

/**
 * "Como venceu": as vitórias por método como uma barra segmentada (a proporção de
 * cada finalização) mais uma legenda com rótulo e contagem — a informação não
 * depende só da cor. Quando não há vitórias, uma nota substitui a barra.
 */
function FinishBreakdown({
  winsByMethod,
}: {
  winsByMethod: Record<string, number>;
}) {
  const segments = finishSegments(winsByMethod);
  const total = segments.reduce((sum, segment) => sum + segment.count, 0);

  return (
    <section>
      <h2 className="mb-4 font-display text-xl font-bold uppercase tracking-wide">
        Como venceu
      </h2>

      {total === 0 ? (
        <p role="status" className="text-sm text-muted-foreground">
          Nenhuma vitória registrada no acervo.
        </p>
      ) : (
        <>
          <div
            aria-hidden="true"
            className="flex h-3 w-full overflow-hidden rounded-full border border-border"
          >
            {segments.map((segment, index) => (
              <span
                key={segment.method}
                className="h-full bg-belt-gold"
                style={{
                  width: `${String((segment.count / total) * 100)}%`,
                  opacity: 1 - index * 0.22,
                }}
              />
            ))}
          </div>

          <ul
            aria-label="Como venceu"
            className="mt-4 grid gap-2 sm:grid-cols-3"
          >
            {segments.map((segment, index) => (
              <li
                key={segment.method}
                className="flex items-baseline justify-between gap-3 border-t border-border pt-2"
              >
                <span className="flex items-center gap-2 text-sm text-muted-foreground">
                  <span
                    aria-hidden="true"
                    className="size-2.5 rounded-full bg-belt-gold"
                    style={{ opacity: 1 - index * 0.22 }}
                  />
                  {segment.label}
                </span>
                <span className="font-mono text-lg font-bold tabular-nums text-belt-gold">
                  {segment.count}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function FighterStatsPageSkeleton() {
  return (
    <section
      className="mx-auto w-full max-w-5xl px-4 py-10"
      aria-busy="true"
      aria-label="Carregando estatísticas"
    >
      <Skeleton className="h-10 w-64" />
      <Skeleton className="mt-8 h-40 w-full" />
    </section>
  );
}

function NotFoundScreen() {
  return (
    <StatusScreen
      icon={<UserX className="size-6 text-muted-foreground" />}
      title="Lutador não encontrado"
      description="Não há nenhum lutador com esse identificador no acervo."
    />
  );
}

function StatusScreen({
  icon,
  title,
  description,
}: {
  icon: ReactNode;
  title: string;
  description: string;
}) {
  return (
    <section className="mx-auto w-full max-w-5xl px-4 py-10">
      <div
        role="status"
        className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/50 px-6 py-16 text-center"
      >
        {icon}
        <p className="font-display text-lg font-semibold uppercase tracking-wide">
          {title}
        </p>
        <p className="max-w-sm text-sm text-muted-foreground">{description}</p>
      </div>
    </section>
  );
}
