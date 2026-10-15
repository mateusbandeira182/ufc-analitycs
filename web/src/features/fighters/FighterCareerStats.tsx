import type { FighterStatsOut } from "@/api/schema";
import { Skeleton } from "@/components/ui/skeleton";
import {
  formatAvgControlTime,
  formatAverage,
} from "@/features/fighters/statsFormat";
import { useFighterStats } from "@/features/fighters/useFighterStats";

/*
  Bloco CARREIRA: o agregado de **todas** as lutas do acervo, computado on demand
  pelo backend em `/fighters/:id/stats`. É a metade arquivística da página — a
  outra, FORMA ATUAL, descreve as três últimas lutas. A RF-24 manda as duas em
  blocos rotulados e separados porque respondem perguntas diferentes, e misturá-las
  faria a tela precisar explicar qual número é de qual família.

  O desenho é o do octógono partido pela linha central: à esquerda o jogo em pé
  (canto vermelho), à direita o jogo no chão (canto azul).
*/

const HEADING_ID = "fighter-career-stats";

/**
 * Seção CARREIRA da página do lutador. Consome o mesmo `useFighterStats` da
 * seção FORMA ATUAL (uma query, dois campos da resposta) e trata os três estados
 * do server-state. O id inválido nunca chega aqui — a página o barra antes.
 */
export function FighterCareerStats({ fighterId }: { fighterId: number }) {
  const { data, isPending, isError } = useFighterStats(fighterId);

  return (
    <section aria-labelledby={HEADING_ID}>
      <h2
        id={HEADING_ID}
        className="font-display text-xl font-bold uppercase tracking-wide"
      >
        Carreira
      </h2>
      <p className="mb-5 mt-1 text-[0.7rem] uppercase tracking-widest text-muted-foreground">
        Médias por luta sobre o acervo inteiro
      </p>

      {isPending ? (
        <div
          aria-busy="true"
          aria-label="Carregando estatísticas de carreira"
          className="grid gap-4"
        >
          <Skeleton className="h-40 w-full" />
        </div>
      ) : isError ? (
        <p role="status" className="text-sm text-muted-foreground">
          Não foi possível carregar as estatísticas deste lutador.
        </p>
      ) : (
        <CareerPanel stats={data} />
      )}
    </section>
  );
}

/**
 * O cartel do atleta partido pela linha central do octógono, com a contagem de
 * lutas que entrou nas médias. Lutador sem luta no acervo recebe uma nota em vez
 * de um painel de traços.
 */
function CareerPanel({ stats }: { stats: FighterStatsOut }) {
  if (stats.bouts_counted === 0) {
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

  return (
    <div className="grid gap-4">
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

      <p className="font-mono text-[0.7rem] uppercase tracking-widest tabular-nums text-muted-foreground">
        {stats.bouts_counted} lutas contabilizadas
      </p>
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
    <div className="relative bg-card p-6 pt-7 sm:p-8 sm:pt-9">
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
    </div>
  );
}
