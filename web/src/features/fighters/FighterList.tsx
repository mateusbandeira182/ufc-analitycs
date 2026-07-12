import type { ReactNode } from "react";

import { AlertTriangle, SearchX } from "lucide-react";
import { Link } from "react-router";

import type { FighterOut } from "@/api/schema";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatRecord, formatStance } from "@/features/fighters/format";

interface FighterListProps {
  fighters: FighterOut[];
  isPending: boolean;
  isError: boolean;
}

/**
 * Renderiza os quatro estados observáveis da lista a partir de props:
 * carregamento (skeleton), erro (mensagem legível), vazio e sucesso.
 * Sem lógica de servidor aqui — os dados vêm do hook via a página.
 */
export function FighterList({
  fighters,
  isPending,
  isError,
}: FighterListProps) {
  if (isPending) {
    return (
      <div
        data-testid="fighters-loading"
        aria-busy="true"
        aria-label="Carregando lutadores"
        className="grid gap-3 sm:grid-cols-2"
      >
        {Array.from({ length: 6 }).map((_, index) => (
          <Skeleton key={index} className="h-24 w-full" />
        ))}
      </div>
    );
  }

  if (isError) {
    return (
      <StatusMessage
        icon={<AlertTriangle className="size-6 text-primary" />}
        title="Não foi possível carregar os lutadores"
        description="A API não respondeu como esperado. Tente novamente em instantes."
      />
    );
  }

  if (fighters.length === 0) {
    return (
      <StatusMessage
        icon={<SearchX className="size-6 text-muted-foreground" />}
        title="Nenhum lutador encontrado"
        description="Ajuste o termo da busca ou limpe o campo para ver todos."
      />
    );
  }

  return (
    <ul aria-label="Lutadores" className="grid gap-3 sm:grid-cols-2">
      {fighters.map((fighter) => (
        <li key={fighter.id}>
          <FighterCard fighter={fighter} />
        </li>
      ))}
    </ul>
  );
}

function FighterCard({ fighter }: { fighter: FighterOut }) {
  return (
    <Link
      to={`/fighters/${String(fighter.id)}`}
      className="group block rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <Card className="relative flex items-center gap-4 overflow-hidden p-4 transition-colors group-hover:border-primary/60">
        {/* Faixa de acento — identidade visual da luta. */}
        <span
          aria-hidden="true"
          className="absolute inset-y-0 left-0 w-1 bg-primary"
        />
        <div className="min-w-0 flex-1 pl-2">
          <h3 className="truncate font-display text-lg font-semibold uppercase tracking-wide">
            {fighter.name}
          </h3>
          {fighter.nickname ? (
            <p className="truncate text-sm italic text-muted-foreground">
              &ldquo;{fighter.nickname}&rdquo;
            </p>
          ) : null}
          <p className="mt-1 text-xs uppercase tracking-wide text-muted-foreground">
            Base: {formatStance(fighter.stance)}
          </p>
        </div>
        <div className="shrink-0 text-right">
          <span className="font-mono text-lg font-bold tabular-nums text-belt-gold">
            {formatRecord(fighter)}
          </span>
          <span className="block text-[0.65rem] uppercase tracking-widest text-muted-foreground">
            V-D-E
          </span>
        </div>
      </Card>
    </Link>
  );
}

function StatusMessage({
  icon,
  title,
  description,
}: {
  icon: ReactNode;
  title: string;
  description: string;
}) {
  return (
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
  );
}
