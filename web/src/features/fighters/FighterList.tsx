import type { ReactNode } from "react";

import { AlertTriangle, SearchX } from "lucide-react";
import { Link } from "react-router";

import type { FighterOut } from "@/api/schema";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  formatHeight,
  formatReach,
  formatRecord,
  formatStance,
} from "@/features/fighters/format";

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

/**
 * Ficha do arquivo: cabeçalho com nome/apelido e o cartel como número-herói em
 * ouro do cinturão; rodapé com a "tale of the tape" (base, altura, alcance). A
 * faixa superior no vermelho do canto e a transição respeitam `prefers-reduced-motion`.
 */
function FighterCard({ fighter }: { fighter: FighterOut }) {
  return (
    <Link
      to={`/fighters/${String(fighter.id)}`}
      className="group block h-full rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <Card className="relative flex h-full flex-col overflow-hidden p-0 transition-colors motion-reduce:transition-none group-hover:border-primary/60">
        {/* Faixa do canto — assinatura do octógono. */}
        <span
          aria-hidden="true"
          className="absolute inset-x-0 top-0 h-0.5 bg-corner-red"
        />
        <div className="flex items-start justify-between gap-4 p-4 pt-5">
          <div className="min-w-0">
            <h3 className="truncate font-display text-lg font-semibold uppercase tracking-wide transition-colors motion-reduce:transition-none group-hover:text-primary">
              {fighter.name}
            </h3>
            {fighter.nickname ? (
              <p className="truncate text-sm italic text-muted-foreground">
                &ldquo;{fighter.nickname}&rdquo;
              </p>
            ) : null}
          </div>
          <div className="shrink-0 text-right">
            <span className="font-mono text-2xl font-bold leading-none tabular-nums text-belt-gold">
              {formatRecord(fighter)}
            </span>
            <span className="mt-1 block text-[0.6rem] uppercase tracking-widest text-muted-foreground">
              Cartel V-D-E
            </span>
          </div>
        </div>

        <dl className="mt-auto grid grid-cols-3 divide-x divide-border border-t border-border">
          <TapeCell label="Base" value={formatStance(fighter.stance)} />
          <TapeCell label="Altura" value={formatHeight(fighter.height_cm)} />
          <TapeCell label="Alcance" value={formatReach(fighter.reach_cm)} />
        </dl>
      </Card>
    </Link>
  );
}

/** Uma medida da "tale of the tape": rótulo em versalete e valor em monoespaçada. */
function TapeCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="px-3 py-2.5 text-center">
      <dt className="text-[0.6rem] uppercase tracking-widest text-muted-foreground">
        {label}
      </dt>
      <dd className="mt-0.5 truncate font-mono text-sm font-medium tabular-nums">
        {value}
      </dd>
    </div>
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
