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
 * Histórico de lutas do lutador, em tabela. Consome o server-state via
 * `useFighterBouts` e renderiza as lutas na ordem cronológica recebida do
 * backend (sem reordenar).
 *
 * Tabela e não lista porque cada linha tem **três** destinos — a luta, o
 * adversário e o evento — e o leitor precisa saber qual é qual. Numa lista, os
 * três links disputavam a mesma linha de texto, e era por isso que o nome do
 * evento levava à luta e não existia caminho nenhum para o evento.
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
    <div className="overflow-x-auto rounded-lg border border-border bg-card">
      <table
        aria-label="Histórico de lutas"
        className="w-full border-collapse text-left text-sm"
      >
        <thead>
          <tr className="border-b border-border">
            <ColumnHeader>Data</ColumnHeader>
            <ColumnHeader>Evento</ColumnHeader>
            <ColumnHeader>Adversário</ColumnHeader>
            <ColumnHeader>Resultado</ColumnHeader>
            <ColumnHeader>Método</ColumnHeader>
            <ColumnHeader>
              <span className="sr-only">Luta</span>
            </ColumnHeader>
          </tr>
        </thead>
        <tbody>
          {data.map((bout) => (
            <BoutRow key={bout.bout_id} bout={bout} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ColumnHeader({ children }: { children: React.ReactNode }) {
  return (
    <th
      scope="col"
      className="px-4 py-3 text-[0.65rem] font-medium uppercase tracking-widest text-muted-foreground"
    >
      {children}
    </th>
  );
}

const LINK_CLASS =
  "rounded-sm hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function BoutRow({ bout }: { bout: FighterBoutOut }) {
  const result = formatResult(bout);

  return (
    <tr className="border-b border-border/60 last:border-b-0">
      <td className="whitespace-nowrap px-4 py-3 font-mono text-xs tabular-nums text-muted-foreground">
        {formatIsoDate(bout.event_date)}
      </td>
      <td className="px-4 py-3 font-display font-semibold uppercase tracking-wide">
        <Link to={`/events/${String(bout.event_id)}`} className={LINK_CLASS}>
          {bout.event_name}
        </Link>
      </td>
      <td className="px-4 py-3">
        {bout.opponent ? (
          <Link
            to={`/fighters/${String(bout.opponent.fighter_id)}`}
            className={LINK_CLASS}
          >
            {bout.opponent.name}
          </Link>
        ) : (
          <span className="text-muted-foreground">Adversário desconhecido</span>
        )}
      </td>
      <td
        className={
          // Vitória ganha o ouro do cinturão; a palavra carrega a informação, a
          // cor só a reforça.
          bout.won
            ? "whitespace-nowrap px-4 py-3 font-display font-bold uppercase tracking-wide text-belt-gold"
            : "whitespace-nowrap px-4 py-3 font-display font-bold uppercase tracking-wide text-muted-foreground"
        }
      >
        {result}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-xs text-muted-foreground">
        {/* `method` nulo é luta sem desfecho: o backend já filtra as agendadas
            (Slice 02), então esta guarda é defesa em profundidade — sem ela, uma
            regressão no filtro exibiria "Derrota" numa luta que não aconteceu. */}
        {formatMethod(bout.method)} ·{" "}
        {formatEndingTime(bout.round, bout.ending_time_seconds)}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-right">
        <Link
          to={`/bouts/${String(bout.bout_id)}`}
          className={`text-xs uppercase tracking-widest text-muted-foreground ${LINK_CLASS}`}
        >
          Ver luta
        </Link>
      </td>
    </tr>
  );
}
