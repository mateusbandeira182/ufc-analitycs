import { AlertTriangle, CalendarDays, Swords, Trophy } from "lucide-react";
import { Link, useParams } from "react-router";

import { ApiError } from "@/api/client";
import type { BoutDetailOut } from "@/api/schema";
import { Skeleton } from "@/components/ui/skeleton";
import { BoutStats } from "@/features/bouts/BoutStats";
import { resolveResult, formatDuration } from "@/features/bouts/format";
import { useBout } from "@/features/bouts/useBout";
import { formatIsoDate, formatMethod } from "@/lib/format";

/**
 * Rota `/bouts/:id`: orquestra o detalhe da luta. Lê o id da URL, trata id
 * inválido e inexistente (404) como não encontrado, delega o server-state ao
 * `useBout` e monta cabeçalho (evento + resultado) + a tabela comparativa de stats.
 */
export function BoutDetail() {
  const { id } = useParams();
  const boutId = Number(id);
  const { data, isPending, isError, error } = useBout(boutId);

  // Id de rota inválido (ex.: `/bouts/abc`): tratado como não encontrado, sem
  // disparar request (o hook fica desabilitado).
  if (Number.isNaN(boutId)) {
    return <NotFoundScreen />;
  }

  if (isPending) {
    return <BoutDetailSkeleton />;
  }

  if (isError) {
    const notFound = error instanceof ApiError && error.status === 404;
    return notFound ? (
      <NotFoundScreen />
    ) : (
      <StatusScreen
        icon={<AlertTriangle className="size-6 text-primary" />}
        title="Não foi possível carregar a luta"
        description="A API não respondeu como esperado. Tente novamente em instantes."
      />
    );
  }

  return (
    <section className="mx-auto w-full max-w-3xl px-4 py-10">
      <BoutHeader bout={data} />
      <div className="mt-10">
        <BoutStats fighters={data.fighters} winnerId={data.winner_id} />
      </div>
    </section>
  );
}

function BoutHeader({ bout }: { bout: BoutDetailOut }) {
  const result = resolveResult(bout);

  return (
    <header>
      <Link
        to={`/events/${String(bout.event.id)}`}
        className="inline-flex items-center gap-1.5 font-display text-xs font-medium uppercase tracking-[0.3em] text-primary hover:text-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <CalendarDays aria-hidden="true" className="size-3.5" />
        {bout.event.name}
      </Link>
      <p className="mt-1 text-xs text-muted-foreground">
        {formatIsoDate(bout.event.date)}
      </p>

      <div
        role="group"
        aria-label="Resultado da luta"
        className="mt-6 rounded-lg border border-border bg-card px-6 py-5"
      >
        <Verdict result={result} />

        <dl className="mt-4 flex flex-wrap gap-x-6 gap-y-2 font-mono text-xs uppercase tracking-widest text-muted-foreground">
          <ResultMeta term="Método" value={formatMethod(bout.method)} />
          {bout.round !== null ? (
            <ResultMeta term="Round" value={`Round ${String(bout.round)}`} />
          ) : null}
          {bout.ending_time_seconds !== null ? (
            <ResultMeta
              term="Tempo"
              value={formatDuration(bout.ending_time_seconds)}
            />
          ) : null}
          {bout.weight_class !== null ? (
            <ResultMeta term="Categoria" value={bout.weight_class} />
          ) : null}
        </dl>
      </div>
    </header>
  );
}

function Verdict({ result }: { result: ReturnType<typeof resolveResult> }) {
  if (result.kind === "winner") {
    return (
      <div className="flex items-center gap-3">
        <Trophy aria-hidden="true" className="size-6 shrink-0 text-belt-gold" />
        <div>
          <p className="text-[0.65rem] font-bold uppercase tracking-[0.3em] text-belt-gold">
            Vencedor
          </p>
          <p className="font-display text-3xl font-bold uppercase leading-tight tracking-tight">
            {result.name}
          </p>
        </div>
      </div>
    );
  }

  const label = result.kind === "draw" ? "Empate" : "Sem vencedor";
  return (
    <div className="flex items-center gap-3">
      <Swords
        aria-hidden="true"
        className="size-6 shrink-0 text-muted-foreground"
      />
      <p className="font-display text-3xl font-bold uppercase leading-tight tracking-tight text-muted-foreground">
        {label}
      </p>
    </div>
  );
}

function ResultMeta({ term, value }: { term: string; value: string }) {
  return (
    <div>
      <dt className="text-[0.6rem] text-muted-foreground/60">{term}</dt>
      <dd className="mt-0.5 text-sm normal-case tracking-normal text-foreground">
        {value}
      </dd>
    </div>
  );
}

function BoutDetailSkeleton() {
  return (
    <section
      className="mx-auto w-full max-w-3xl px-4 py-10"
      aria-busy="true"
      aria-label="Carregando luta"
    >
      <Skeleton className="h-4 w-40" />
      <Skeleton className="mt-6 h-28 w-full" />
      <Skeleton className="mt-8 h-64 w-full" />
    </section>
  );
}

function NotFoundScreen() {
  return (
    <StatusScreen
      icon={<Swords className="size-6 text-muted-foreground" />}
      title="Luta não encontrada"
      description="Não há nenhuma luta com esse identificador no acervo."
    />
  );
}

function StatusScreen({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <section className="mx-auto w-full max-w-3xl px-4 py-10">
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
