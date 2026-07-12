import { ArrowRight, Handshake, Trophy } from "lucide-react";
import { Link } from "react-router";

import type { BoutDetailOut, FighterOut } from "@/api/schema";
import { Card } from "@/components/ui/card";
import { FighterRecord } from "@/features/fighters/FighterRecord";
import { formatEndingTime, formatIsoDate, formatMethod } from "@/lib/format";

interface HeadToHeadComparisonProps {
  fighterA: FighterOut;
  fighterB: FighterOut;
  /** Confrontos diretos em ordem cronológica; vazio => nunca se enfrentaram. */
  bouts: BoutDetailOut[];
}

/**
 * Comparação presentacional: os dois lutadores frente a frente (canto vermelho à
 * esquerda, azul à direita — o vernáculo do octógono) com cartel e atributos, e
 * abaixo o retrospecto do confronto direto. Sem confronto, indica-o em vez de
 * deixar a seção vazia sem explicação. Os dados vêm da rota.
 */
export function HeadToHeadComparison({
  fighterA,
  fighterB,
  bouts,
}: HeadToHeadComparisonProps) {
  return (
    <div className="space-y-10">
      <div className="grid gap-4 lg:grid-cols-[1fr_auto_1fr] lg:items-center">
        <FighterColumn fighter={fighterA} corner="red" />
        <VersusEmblem />
        <FighterColumn fighter={fighterB} corner="blue" />
      </div>

      <section aria-labelledby="direct-bouts-heading">
        <h2
          id="direct-bouts-heading"
          className="mb-4 font-display text-xl font-bold uppercase tracking-wide"
        >
          Confronto direto
        </h2>
        {bouts.length === 0 ? (
          <NeverFought fighterA={fighterA} fighterB={fighterB} />
        ) : (
          <ul aria-label="Confrontos diretos" className="grid gap-3">
            {bouts.map((bout) => (
              <DirectBout key={bout.id} bout={bout} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function FighterColumn({
  fighter,
  corner,
}: {
  fighter: FighterOut;
  corner: "red" | "blue";
}) {
  const cornerAccent =
    corner === "red" ? "text-corner-red" : "text-corner-blue";
  const cornerLabel = corner === "red" ? "Canto vermelho" : "Canto azul";

  return (
    <div>
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="min-w-0 truncate font-display text-2xl font-bold uppercase tracking-tight">
          <Link
            to={`/fighters/${String(fighter.id)}`}
            className="hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {fighter.name}
          </Link>
        </h2>
        <span
          className={`shrink-0 text-[0.6rem] font-medium uppercase tracking-[0.2em] ${cornerAccent}`}
        >
          {cornerLabel}
        </span>
      </div>
      <FighterRecord fighter={fighter} />
    </div>
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

function NeverFought({
  fighterA,
  fighterB,
}: {
  fighterA: FighterOut;
  fighterB: FighterOut;
}) {
  return (
    <div
      role="status"
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center"
    >
      <Handshake aria-hidden="true" className="size-6 text-primary" />
      <p className="font-display text-lg font-semibold uppercase tracking-wide">
        Nunca se enfrentaram
      </p>
      <p className="max-w-sm text-sm text-muted-foreground">
        {fighterA.name} e {fighterB.name} não têm confronto direto registrado no
        acervo.
      </p>
    </div>
  );
}

function VersusEmblem() {
  return (
    <div aria-hidden="true" className="hidden place-items-center px-4 lg:grid">
      <span className="octagon-clip grid size-12 place-items-center bg-gradient-to-br from-corner-red via-primary to-corner-blue p-[2px]">
        <span className="octagon-clip grid size-full place-items-center bg-card font-display text-sm font-bold uppercase tracking-widest text-belt-gold">
          vs
        </span>
      </span>
    </div>
  );
}
