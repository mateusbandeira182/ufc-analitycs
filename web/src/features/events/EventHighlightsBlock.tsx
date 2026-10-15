import type { ReactNode } from "react";

import { Link } from "react-router";

import type { EventHighlightsOut } from "@/api/schema";
import { formatEndingTime } from "@/lib/format";

interface EventHighlightsBlockProps {
  /** `null` quando o evento não tem luta realizada classificável. */
  highlights: EventHighlightsOut | null;
}

/**
 * Os quatro destaques **calculados** do granular. O rótulo "calculado, não
 * oficial" não é modéstia: premiação de evento não existe em nenhuma das duas
 * fontes (`awards` da Cito é sempre nulo e a fonte oficial não publica), então
 * estes números são uma leitura do box-score, não o prêmio da noite.
 *
 * Destaque nulo é **omitido**, nunca exibido como zero ou traço mudo: ausência
 * de base é ausência. Um `value: 0` que chega preenchido é medição — ninguém
 * acertou queda nenhuma naquele card — e aparece como 0.
 *
 * Quando nenhum dos quatro tem base, o bloco inteiro desaparece: um cabeçalho
 * "Destaques" sobre lista vazia seria bloco vazio e mudo.
 */
export function EventHighlightsBlock({
  highlights,
}: EventHighlightsBlockProps) {
  if (highlights === null) {
    return null;
  }

  const { fastest_finish, most_sig_strikes, most_takedowns, biggest_upset } =
    highlights;

  const items: ReactNode[] = [];

  if (fastest_finish) {
    items.push(
      <HighlightItem
        key="fastest"
        label="Finalização mais rápida"
        value={formatEndingTime(
          fastest_finish.round,
          fastest_finish.ending_time_seconds,
        )}
        fighterId={fastest_finish.fighter.id}
        fighterName={fastest_finish.fighter.name}
        boutId={fastest_finish.bout_id}
      />,
    );
  }

  if (most_sig_strikes) {
    items.push(
      <HighlightItem
        key="strikes"
        label="Mais golpes significativos"
        value={String(most_sig_strikes.value)}
        fighterId={most_sig_strikes.fighter.id}
        fighterName={most_sig_strikes.fighter.name}
        boutId={most_sig_strikes.bout_id}
      />,
    );
  }

  if (most_takedowns) {
    items.push(
      <HighlightItem
        key="takedowns"
        label="Mais quedas"
        value={String(most_takedowns.value)}
        fighterId={most_takedowns.fighter.id}
        fighterName={most_takedowns.fighter.name}
        boutId={most_takedowns.bout_id}
      />,
    );
  }

  if (biggest_upset) {
    items.push(
      <HighlightItem
        key="upset"
        label="Maior zebra"
        value={`${String(Math.round(biggest_upset.prob_of_winner * 100))}%`}
        fighterId={biggest_upset.winner.id}
        fighterName={biggest_upset.winner.name}
        boutId={biggest_upset.bout_id}
        note={`${String(biggest_upset.n_bouts_with_prediction)} de ${String(biggest_upset.n_bouts)} lutas previstas`}
      />,
    );
  }

  if (items.length === 0) {
    return null;
  }

  return (
    <section aria-labelledby="evento-destaques">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2
          id="evento-destaques"
          className="font-display text-sm font-bold uppercase tracking-[0.2em]"
        >
          Destaques da noite
        </h2>
        <p className="font-display text-[0.65rem] font-medium uppercase tracking-[0.2em] text-belt-gold">
          Calculado, não oficial
        </p>
      </div>

      <dl className="grid gap-px overflow-hidden rounded-lg border border-border bg-border sm:grid-cols-2 lg:grid-cols-4">
        {items}
      </dl>
    </section>
  );
}

/**
 * Um destaque: o rótulo, o número em monoespaçada (a tipografia dos dados neste
 * app) e o lutador linkado. A nota existe só na zebra, que carrega o próprio
 * denominador — "entre as 9 de 13 lutas previstas" é parte da afirmação.
 */
function HighlightItem({
  label,
  value,
  fighterId,
  fighterName,
  boutId,
  note,
}: {
  label: string;
  value: string;
  fighterId: number;
  fighterName: string;
  boutId: number;
  note?: string;
}) {
  return (
    <div className="flex flex-col gap-1.5 bg-card px-4 py-3">
      <dt className="font-display text-[0.65rem] font-medium uppercase tracking-widest text-muted-foreground">
        {label}
      </dt>
      <dd className="flex flex-col gap-0.5">
        <span className="font-mono text-2xl font-bold leading-none tabular-nums text-belt-gold">
          {value}
        </span>
        <Link
          to={`/fighters/${String(fighterId)}`}
          className="truncate font-display text-sm font-semibold uppercase tracking-wide hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {fighterName}
        </Link>
        <Link
          to={`/bouts/${String(boutId)}`}
          className="w-fit text-[0.7rem] text-muted-foreground underline-offset-4 hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {note ?? "Ver luta"}
        </Link>
      </dd>
    </div>
  );
}
