import type { ReactNode } from "react";

import { AlertTriangle, BarChart3, UserX } from "lucide-react";
import { Link, useParams } from "react-router";

import { ApiError } from "@/api/client";
import { Skeleton } from "@/components/ui/skeleton";
import { FighterBoutHistory } from "@/features/fighters/FighterBoutHistory";
import { FighterRecord } from "@/features/fighters/FighterRecord";
import { useFighter } from "@/features/fighters/useFighter";

/**
 * Rota `/fighters/:id`: orquestra o detalhe do lutador. Lê o id da URL, delega
 * o server-state ao `useFighter` e monta cabeçalho + cartel + histórico,
 * tratando carregamento, erro genérico e lutador inexistente (404).
 */
export function FighterPage() {
  const { id } = useParams();
  const fighterId = Number(id);
  const { data, isPending, isError, error } = useFighter(fighterId);

  // Id de rota inválido (ex.: `/fighters/abc`): tratado como não encontrado, sem
  // disparar request (o hook fica desabilitado). Consistente com /events e /bouts.
  if (Number.isNaN(fighterId)) {
    return (
      <StatusScreen
        icon={<UserX className="size-6 text-muted-foreground" />}
        title="Lutador não encontrado"
        description="Não há nenhum lutador com esse identificador no acervo."
      />
    );
  }

  if (isPending) {
    return <FighterPageSkeleton />;
  }

  if (isError) {
    const notFound = error instanceof ApiError && error.status === 404;
    return notFound ? (
      <StatusScreen
        icon={<UserX className="size-6 text-muted-foreground" />}
        title="Lutador não encontrado"
        description="Não há nenhum lutador com esse identificador no acervo."
      />
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
      <header className="mb-6">
        <h1 className="font-display text-4xl font-bold uppercase tracking-tight">
          {data.name}
        </h1>
        {data.nickname ? (
          <p className="mt-1 text-lg italic text-muted-foreground">
            &ldquo;{data.nickname}&rdquo;
          </p>
        ) : null}
      </header>

      <FighterRecord fighter={data} />

      <Link
        to={`/fighters/${String(fighterId)}/stats`}
        className="mt-4 inline-flex items-center gap-2 rounded-md border border-border bg-card px-4 py-2 font-display text-sm font-semibold uppercase tracking-wide hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <BarChart3 aria-hidden="true" className="size-4" />
        Estatísticas do atleta
      </Link>

      <div className="mt-10">
        <h2 className="mb-4 font-display text-xl font-bold uppercase tracking-wide">
          Histórico
        </h2>
        <FighterBoutHistory fighterId={fighterId} />
      </div>
    </section>
  );
}

function FighterPageSkeleton() {
  return (
    <section
      className="mx-auto w-full max-w-5xl px-4 py-10"
      aria-busy="true"
      aria-label="Carregando lutador"
    >
      <Skeleton className="h-10 w-64" />
      <Skeleton className="mt-6 h-32 w-full" />
    </section>
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
