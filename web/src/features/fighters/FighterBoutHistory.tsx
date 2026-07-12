import { Link } from "react-router";

import type { FighterBoutOut } from "@/api/schema";
import { Skeleton } from "@/components/ui/skeleton";
import {
  formatEndingTime,
  formatIsoDate,
  formatMethod,
  formatResult,
} from "@/features/fighters/format";
import { useFighterBouts } from "@/features/fighters/useFighterBouts";

interface FighterBoutHistoryProps {
  fighterId: number;
}

/**
 * Histórico de lutas do lutador. Consome o server-state via `useFighterBouts` e
 * renderiza as lutas na ordem cronológica recebida do backend (sem reordenar).
 */
export function FighterBoutHistory({ fighterId }: FighterBoutHistoryProps) {
  const { data, isPending, isError } = useFighterBouts(fighterId);

  if (isPending) {
    return (
      <div
        aria-busy="true"
        aria-label="Carregando histórico"
        className="grid gap-2"
      >
        {Array.from({ length: 3 }).map((_, index) => (
          <Skeleton key={index} className="h-16 w-full" />
        ))}
      </div>
    );
  }

  if (isError) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Não foi possível carregar o histórico de lutas.
      </p>
    );
  }

  if (data.length === 0) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Nenhuma luta registrada para este lutador.
      </p>
    );
  }

  return (
    <ul aria-label="Histórico de lutas" className="grid gap-2">
      {data.map((bout) => (
        <li key={bout.bout_id}>
          <BoutRow bout={bout} />
        </li>
      ))}
    </ul>
  );
}

function BoutRow({ bout }: { bout: FighterBoutOut }) {
  const result = formatResult(bout);
  const won = bout.won;

  return (
    <div className="relative flex items-center gap-4 overflow-hidden rounded-lg border border-border bg-card p-4 pl-5">
      {/* Vitória ganha o ouro do cinturão; derrota e sem-resultado ficam neutros. */}
      <span
        aria-hidden="true"
        className={
          won
            ? "absolute inset-y-0 left-0 w-1 bg-belt-gold"
            : "absolute inset-y-0 left-0 w-1 bg-border"
        }
      />
      <div className="min-w-0 flex-1">
        {/* A rota /bouts/:id é destino da Slice 04; o link já é válido aqui. */}
        <Link
          to={`/bouts/${String(bout.bout_id)}`}
          className="truncate font-display text-base font-semibold uppercase tracking-wide hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {bout.event_name}
        </Link>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {formatIsoDate(bout.event_date)} · vs{" "}
          {bout.opponent ? (
            <Link
              to={`/fighters/${String(bout.opponent.fighter_id)}`}
              className="text-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {bout.opponent.name}
            </Link>
          ) : (
            <span>Adversário desconhecido</span>
          )}
        </p>
      </div>
      <div className="shrink-0 text-right">
        <span
          className={
            won
              ? "font-display text-sm font-bold uppercase tracking-wide text-belt-gold"
              : "font-display text-sm font-bold uppercase tracking-wide text-muted-foreground"
          }
        >
          {result}
        </span>
        <span className="block text-[0.65rem] uppercase tracking-widest text-muted-foreground">
          {formatMethod(bout.method)} ·{" "}
          {formatEndingTime(bout.round, bout.ending_time_seconds)}
        </span>
      </div>
    </div>
  );
}
