import { ArrowRight, Handshake, Trophy } from "lucide-react";
import { Link } from "react-router";

import type { BoutDetailOut } from "@/api/schema";
import { Card } from "@/components/ui/card";
import { formatEndingTime, formatIsoDate, formatMethod } from "@/lib/format";

interface DirectBoutsProps {
  /** Confrontos diretos em ordem cronológica; vazio => nunca se enfrentaram. */
  bouts: BoutDetailOut[];
  nameA: string;
  nameB: string;
}

/**
 * Retrospecto do confronto direto da dupla. Extraído de `HeadToHeadComparison`
 * (SPEC 011, RF-23) com o comportamento preservado: a lista com resultado,
 * método, momento e o link para o detalhe da luta, e o estado "nunca se
 * enfrentaram" nomeando os dois atletas em vez de deixar a seção vazia sem
 * explicação.
 */
export function DirectBouts({ bouts, nameA, nameB }: DirectBoutsProps) {
  return (
    <section aria-labelledby="direct-bouts-heading">
      <h2
        id="direct-bouts-heading"
        className="font-display text-xl font-bold uppercase tracking-wide"
      >
        Confronto direto
      </h2>
      {bouts.length === 0 ? (
        <NeverFought nameA={nameA} nameB={nameB} />
      ) : (
        <ul aria-label="Confrontos diretos" className="mt-5 grid gap-3">
          {bouts.map((bout) => (
            <DirectBout key={bout.id} bout={bout} />
          ))}
        </ul>
      )}
    </section>
  );
}

function DirectBout({ bout }: { bout: BoutDetailOut }) {
  const winner =
    bout.winner_id === null
      ? null
      : (bout.fighters.find((f) => f.fighter_id === bout.winner_id) ?? null);

  return (
    <li aria-label={bout.event.name}>
      <Card className="overflow-hidden p-0">
        <div className="flex flex-col gap-1 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
            <p className="font-display text-base font-semibold uppercase tracking-wide">
              {bout.event.name}
            </p>
            <p className="font-mono text-xs tabular-nums text-muted-foreground">
              {formatIsoDate(bout.event.date)}
            </p>
          </div>
          <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm">
            {winner ? (
              <span className="inline-flex items-center gap-1.5 font-medium text-belt-gold">
                <Trophy aria-hidden="true" className="size-4" />
                {winner.name}
              </span>
            ) : (
              <span className="font-medium text-muted-foreground">
                Sem vencedor
              </span>
            )}
            <span aria-hidden="true" className="text-muted-foreground">
              ·
            </span>
            <span className="text-muted-foreground">
              {formatMethod(bout.method)}
            </span>
            <span aria-hidden="true" className="text-muted-foreground">
              ·
            </span>
            <span className="text-muted-foreground">
              {formatEndingTime(bout.round, bout.ending_time_seconds)}
            </span>
            {bout.weight_class ? (
              <>
                <span aria-hidden="true" className="text-muted-foreground">
                  ·
                </span>
                <span className="text-muted-foreground">
                  {bout.weight_class}
                </span>
              </>
            ) : null}
          </p>
        </div>
        <div className="flex justify-end border-t border-border bg-secondary/40 px-4 py-2">
          <Link
            to={`/bouts/${String(bout.id)}`}
            className="inline-flex items-center gap-1 text-xs font-medium uppercase tracking-wide text-muted-foreground transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Ver luta
            <ArrowRight aria-hidden="true" className="size-3.5" />
          </Link>
        </div>
      </Card>
    </li>
  );
}

function NeverFought({ nameA, nameB }: { nameA: string; nameB: string }) {
  return (
    <div
      role="status"
      className="mt-5 flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center"
    >
      <Handshake aria-hidden="true" className="size-6 text-primary" />
      <p className="font-display text-lg font-semibold uppercase tracking-wide">
        Nunca se enfrentaram
      </p>
      <p className="max-w-sm text-sm text-muted-foreground">
        {nameA} e {nameB} não têm confronto direto registrado no acervo.
      </p>
    </div>
  );
}
