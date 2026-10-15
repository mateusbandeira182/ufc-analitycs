import type { ReactNode } from "react";

import { AlertTriangle, SearchX } from "lucide-react";
import { Link } from "react-router";

import type { FighterOut } from "@/api/schema";
import { Avatar } from "@/components/ui/avatar";
import { Skeleton } from "@/components/ui/skeleton";
import {
  formatHeight,
  formatReach,
  formatRecord,
  formatStance,
} from "@/features/fighters/format";

interface FightersTableProps {
  fighters: FighterOut[];
  isPending: boolean;
  isError: boolean;
}

/**
 * A "tale of the tape" do acervo inteiro: uma linha por lutador, com retrato,
 * cartel e as três medidas que a tabela do esporte sempre traz. Renderiza os
 * quatro estados observáveis a partir de props — carregamento, erro, vazio e
 * sucesso. Sem server-state aqui: os dados vêm do hook, pela página.
 */
export function FightersTable({
  fighters,
  isPending,
  isError,
}: FightersTableProps) {
  if (isPending) {
    return (
      <div
        data-testid="fighters-loading"
        aria-busy="true"
        aria-label="Carregando lutadores"
        className="grid gap-2"
      >
        {Array.from({ length: 8 }).map((_, index) => (
          <Skeleton key={index} className="h-14 w-full" />
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
        description="Busque pelo nome ou pelo apelido, ou limpe o campo para ver todos."
      />
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-card">
      <table className="w-full border-collapse text-left">
        <caption className="sr-only">Lutadores</caption>
        <thead>
          {/* A faixa do canto vermelho separa o cabeçalho do acervo. */}
          <tr className="border-b-2 border-corner-red/70">
            <ColumnHeader>Lutador</ColumnHeader>
            <ColumnHeader className="text-right">Cartel</ColumnHeader>
            <ColumnHeader className="hidden text-right sm:table-cell">
              Altura
            </ColumnHeader>
            <ColumnHeader className="hidden text-right sm:table-cell">
              Alcance
            </ColumnHeader>
            <ColumnHeader className="hidden text-right md:table-cell">
              Base
            </ColumnHeader>
          </tr>
        </thead>
        <tbody>
          {fighters.map((fighter) => (
            <tr
              key={fighter.id}
              className="border-b border-border/60 transition-colors last:border-b-0 motion-reduce:transition-none hover:bg-secondary/40"
            >
              <th scope="row" className="px-4 py-3 font-normal">
                <div className="flex items-center gap-3">
                  <Avatar
                    name={fighter.name}
                    src={fighter.headshot_url}
                    className="size-9"
                  />
                  <div className="min-w-0">
                    <Link
                      to={`/fighters/${String(fighter.id)}`}
                      className="block truncate font-display text-base font-semibold uppercase tracking-wide transition-colors motion-reduce:transition-none hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                    >
                      {fighter.name}
                    </Link>
                    {fighter.nickname ? (
                      <span className="block truncate text-xs italic text-muted-foreground">
                        &ldquo;{fighter.nickname}&rdquo;
                      </span>
                    ) : null}
                  </div>
                </div>
              </th>
              <td className="px-4 py-3 text-right font-mono text-sm font-bold tabular-nums text-belt-gold">
                {formatRecord(fighter)}
              </td>
              <TapeCell className="hidden sm:table-cell">
                {formatHeight(fighter.height_cm)}
              </TapeCell>
              <TapeCell className="hidden sm:table-cell">
                {formatReach(fighter.reach_cm)}
              </TapeCell>
              <TapeCell className="hidden md:table-cell">
                {formatStance(fighter.stance)}
              </TapeCell>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ColumnHeader({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <th
      scope="col"
      className={`px-4 py-3 font-display text-[0.65rem] font-semibold uppercase tracking-[0.2em] text-muted-foreground ${className ?? ""}`}
    >
      {children}
    </th>
  );
}

/** Uma medida da "tale of the tape": monoespaçada, tabular, alinhada à direita. */
function TapeCell({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <td
      className={`px-4 py-3 text-right font-mono text-sm tabular-nums text-muted-foreground ${className ?? ""}`}
    >
      {children}
    </td>
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
