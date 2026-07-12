import { ArrowRight, Trophy } from "lucide-react";
import { Fragment } from "react";
import { Link } from "react-router";

import type { BoutCardFighterOut, BoutCardOut } from "@/api/schema";
import { Card } from "@/components/ui/card";
import { formatEndingTime, formatMethod } from "@/lib/format";

interface EventBoutCardProps {
  bout: BoutCardOut;
}

/**
 * Card de uma luta do evento: os dois cantos como "A vs B", o vencedor destacado
 * (Trophy + ouro do cinturão) e o resultado. Cada lutador linka para o seu
 * detalhe; a luta inteira linka para `/bouts/:id`. O card já traz os dois
 * participantes — não é preciso buscar lutador por luta.
 */
export function EventBoutCard({ bout }: EventBoutCardProps) {
  // Ordena por canto (vermelho primeiro) para exibição determinística — não
  // confia na ordem do array da resposta.
  const ordered = [...bout.fighters].sort((a, b) =>
    a.corner === b.corner ? 0 : a.corner === "red" ? -1 : 1,
  );
  const matchup = ordered.map((fighter) => fighter.name).join(" vs ");

  return (
    <li aria-label={matchup}>
      <Card className="overflow-hidden p-0">
        <div className="grid grid-cols-[1fr_auto_1fr] items-stretch">
          {ordered.map((fighter, index) => (
            <Fragment key={fighter.fighter_id}>
              {index > 0 ? <VersusDivider /> : null}
              <FighterCorner
                fighter={fighter}
                isWinner={bout.winner_id === fighter.fighter_id}
              />
            </Fragment>
          ))}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border bg-secondary/40 px-4 py-2.5">
          <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[0.7rem] uppercase tracking-widest text-muted-foreground">
            <span className="text-foreground">{formatMethod(bout.method)}</span>
            <span aria-hidden="true">·</span>
            <span>
              {formatEndingTime(bout.round, bout.ending_time_seconds)}
            </span>
            {bout.weight_class ? (
              <>
                <span aria-hidden="true">·</span>
                <span>{bout.weight_class}</span>
              </>
            ) : null}
          </p>
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

function FighterCorner({
  fighter,
  isWinner,
}: {
  fighter: BoutCardFighterOut;
  isWinner: boolean;
}) {
  const cornerAccent =
    fighter.corner === "red" ? "bg-corner-red" : "bg-corner-blue";

  return (
    <div className="relative flex flex-col gap-1 p-4">
      {/* Faixa do canto: vermelho ou azul, o vernáculo do octógono. */}
      <span
        aria-hidden="true"
        className={`absolute inset-x-0 top-0 h-1 ${cornerAccent}`}
      />
      <span className="text-[0.6rem] font-medium uppercase tracking-[0.2em] text-muted-foreground">
        {fighter.corner === "red" ? "Canto vermelho" : "Canto azul"}
      </span>
      <Link
        to={`/fighters/${String(fighter.fighter_id)}`}
        aria-label={isWinner ? `${fighter.name}, vencedor` : undefined}
        className="font-display text-base font-semibold uppercase tracking-wide hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {fighter.name}
      </Link>
      {isWinner ? (
        <span
          aria-hidden="true"
          className="inline-flex w-fit items-center gap-1 rounded-sm bg-belt-gold/15 px-1.5 py-0.5 text-[0.6rem] font-bold uppercase tracking-widest text-belt-gold"
        >
          <Trophy className="size-3" />
          Vencedor
        </span>
      ) : null}
    </div>
  );
}

function VersusDivider() {
  return (
    <div className="flex items-center justify-center px-2">
      <span className="font-display text-xs font-bold uppercase tracking-widest text-primary">
        vs
      </span>
    </div>
  );
}
