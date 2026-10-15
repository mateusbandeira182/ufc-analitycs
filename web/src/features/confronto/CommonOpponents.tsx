import { Handshake, Minus, Trophy, X } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import type {
  CommonOpponentBoutOut,
  CommonOpponentOut,
  MatchupFighterOut,
} from "@/api/schema";
import { Avatar } from "@/components/ui/avatar";
import { Card } from "@/components/ui/card";
import { formatIsoDate, formatMethod } from "@/lib/format";

interface CommonOpponentsProps {
  opponents: CommonOpponentOut[];
  /*
    Os dois lados com **id e nome**: o desfecho de cada luta é derivado
    comparando `winner_id` com o id do lado, e o nome é o que a tela exibe.
    Identificar o lado pelo nome não funcionaria — nome não é chave.
  */
  sideA: MatchupFighterOut;
  sideB: MatchupFighterOut;
}

/**
 * Adversários que os dois atletas enfrentaram, com o desfecho de **cada lado**
 * contra o mesmo terceiro. É a seção que faz a página servir para a dupla que
 * nunca se enfrentou — o caso comum.
 *
 * Presentacional puro; o próprio par nunca aparece aqui (o backend o exclui).
 */
export function CommonOpponents({
  opponents,
  sideA,
  sideB,
}: CommonOpponentsProps) {
  return (
    <section aria-labelledby="adversarios-heading">
      <h2
        id="adversarios-heading"
        className="font-display text-xl font-bold uppercase tracking-wide"
      >
        Adversários em comum
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Como cada um se saiu contra quem os dois já enfrentaram.
      </p>

      {opponents.length === 0 ? (
        <NoCommonOpponents nameA={sideA.name} nameB={sideB.name} />
      ) : (
        <ul aria-label="Adversários em comum" className="mt-5 grid gap-4">
          {opponents.map((entry) => (
            <OpponentBlock
              key={entry.opponent.id}
              entry={entry}
              sideA={sideA}
              sideB={sideB}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function OpponentBlock({
  entry,
  sideA,
  sideB,
}: {
  entry: CommonOpponentOut;
  sideA: MatchupFighterOut;
  sideB: MatchupFighterOut;
}) {
  const { opponent } = entry;

  return (
    <li aria-label={opponent.name}>
      <Card className="p-0">
        <div className="flex items-center gap-3 border-b border-border px-4 py-3">
          <Avatar name={opponent.name} className="size-9" />
          <p className="min-w-0 truncate font-display text-base font-semibold uppercase tracking-wide">
            <Link
              to={`/fighters/${String(opponent.id)}`}
              className="hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {opponent.name}
            </Link>
          </p>
        </div>
        <div className="grid gap-x-4 gap-y-4 px-4 py-3 sm:grid-cols-2">
          <SideResults
            side={sideA}
            accent="text-corner-red"
            opponentName={opponent.name}
            bouts={entry.bouts_a}
          />
          <SideResults
            side={sideB}
            accent="text-corner-blue"
            opponentName={opponent.name}
            bouts={entry.bouts_b}
          />
        </div>
      </Card>
    </li>
  );
}

/** O que um dos lados fez contra este adversário — uma linha por luta. */
function SideResults({
  side,
  accent,
  opponentName,
  bouts,
}: {
  side: MatchupFighterOut;
  accent: string;
  opponentName: string;
  bouts: CommonOpponentBoutOut[];
}) {
  return (
    <div>
      <p
        className={`mb-2 truncate font-display text-[0.65rem] font-semibold uppercase tracking-[0.2em] ${accent}`}
      >
        {side.name}
      </p>
      {bouts.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Não enfrentou {opponentName}.
        </p>
      ) : (
        <ul
          aria-label={`Lutas de ${side.name} contra ${opponentName}`}
          className="grid gap-2"
        >
          {bouts.map((bout) => (
            <BoutLine key={bout.bout_id} bout={bout} side={side} />
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Uma luta contra o adversário em comum. O desfecho é derivado de `winner_id`
 * com **três** saídas, não duas: igual ao lado é vitória, `null` é empate ou no
 * contest, qualquer outro é derrota. Colapsar `null` em derrota é o defeito que
 * esta SPA já cometeu uma vez.
 */
function BoutLine({
  bout,
  side,
}: {
  bout: CommonOpponentBoutOut;
  side: MatchupFighterOut;
}) {
  const outcome = outcomeOf(bout, side);

  return (
    <li aria-label={bout.event_name} className="text-sm">
      <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        <span
          className={`inline-flex items-center gap-1.5 font-medium ${outcome.className}`}
        >
          {outcome.icon}
          {outcome.label}
        </span>
        <span aria-hidden="true" className="text-muted-foreground">
          ·
        </span>
        <Link
          to={`/bouts/${String(bout.bout_id)}`}
          className="truncate text-muted-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {bout.event_name}
        </Link>
      </p>
      <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
        <span className="font-mono tabular-nums">
          {formatIsoDate(bout.event_date)}
        </span>
        <span aria-hidden="true">·</span>
        <span>{formatMethod(bout.method)}</span>
      </p>
    </li>
  );
}

/** Rótulo, ícone e cor do desfecho desta luta para o lado consultado. */
function outcomeOf(
  bout: CommonOpponentBoutOut,
  side: MatchupFighterOut,
): { label: string; icon: ReactNode; className: string } {
  if (bout.winner_id === null) {
    return {
      label: "Sem vencedor",
      icon: <Minus aria-hidden="true" className="size-4" />,
      className: "text-muted-foreground",
    };
  }
  return bout.winner_id === side.id
    ? {
        label: "Venceu",
        icon: <Trophy aria-hidden="true" className="size-4" />,
        className: "text-belt-gold",
      }
    : {
        label: "Perdeu",
        icon: <X aria-hidden="true" className="size-4" />,
        className: "text-muted-foreground",
      };
}

function NoCommonOpponents({ nameA, nameB }: { nameA: string; nameB: string }) {
  return (
    <div
      role="status"
      className="mt-5 flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center"
    >
      <Handshake aria-hidden="true" className="size-6 text-primary" />
      <p className="font-display text-lg font-semibold uppercase tracking-wide">
        Nenhum adversário em comum
      </p>
      <p className="max-w-sm text-sm text-muted-foreground">
        {nameA} e {nameB} não têm, no acervo, nenhum atleta que os dois tenham
        enfrentado.
      </p>
    </div>
  );
}
