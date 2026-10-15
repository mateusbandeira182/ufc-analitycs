import { Trophy } from "lucide-react";

import type { FighterOut, MatchupPredictionOut } from "@/api/schema";
import { StaleFeaturesBadge } from "@/components/StaleFeaturesBadge";
import { UnavailablePrediction } from "@/components/UnavailablePrediction";
import { Avatar } from "@/components/ui/avatar";
import {
  clampProbability,
  formatProbability,
} from "@/features/confronto/format";

interface PredictionSectionProps {
  prediction: MatchupPredictionOut;
  /** Detalhe dos dois lados (retrato e cartel) — `MatchupFighterOut` só traz id e nome. */
  fighterA: FighterOut;
  fighterB: FighterOut;
}

/**
 * Seção de previsão do confronto: o frente-a-frente, o vencedor previsto em
 * destaque e as duas probabilidades complementares.
 *
 * O aviso de dado defasado aparece nos **dois** ramos, inclusive no
 * indisponível: é o `features_freshness` que distingue "estreante" de "cache
 * nunca materializado", e esconder o aviso ali deixaria o usuário sem o
 * discriminador das duas causas.
 *
 * Indisponível é `unavailable_code !== null || prob_a_wins === null ||
 * prob_b_wins === null`: `clampProbability(null)` renderizaria "0%", uma
 * afirmação sobre uma luta que ninguém mediu. O grupo rotulado existe para que o
 * teste possa afirmar a **ausência** de barra e porcentagem por nome, em vez de
 * varrer a tela por "%" e acertar por acidente.
 */
export function PredictionSection({
  prediction,
  fighterA,
  fighterB,
}: PredictionSectionProps) {
  const { prob_a_wins, prob_b_wins } = prediction;

  return (
    <section aria-labelledby="previsao-heading" className="grid gap-5">
      <div>
        <h2
          id="previsao-heading"
          className="font-display text-xl font-bold uppercase tracking-wide"
        >
          Previsão do modelo
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Palpite neutro de canto: a ordem dos atletas não muda o resultado.
        </p>
      </div>

      <StaleFeaturesBadge freshness={prediction.features_freshness} />

      {prediction.unavailable_code !== null ||
      prob_a_wins === null ||
      prob_b_wins === null ? (
        <UnavailablePrediction
          code={prediction.unavailable_code}
          reason={prediction.unavailable_reason}
        />
      ) : (
        <AvailablePrediction
          fighterA={fighterA}
          fighterB={fighterB}
          probA={prob_a_wins}
          probB={prob_b_wins}
          winnerIsA={prediction.predicted_winner_id === prediction.fighter_a.id}
        />
      )}
    </section>
  );
}

function AvailablePrediction({
  fighterA,
  fighterB,
  probA,
  probB,
  winnerIsA,
}: {
  fighterA: FighterOut;
  fighterB: FighterOut;
  probA: number;
  probB: number;
  winnerIsA: boolean;
}) {
  const winner = winnerIsA ? fighterA : fighterB;

  return (
    <div className="grid gap-6">
      <div
        role="status"
        aria-label="Vencedor previsto"
        className="flex flex-col items-center gap-3 rounded-lg border border-primary/40 bg-card px-6 py-8 text-center"
      >
        <Avatar
          name={winner.name}
          src={winner.headshot_url}
          className="size-16"
        />
        <p className="font-display text-xs font-medium uppercase tracking-[0.3em] text-primary">
          <Trophy aria-hidden="true" className="mr-1.5 inline size-3.5" />
          Vencedor previsto
        </p>
        <h3 className="font-display text-3xl font-bold uppercase tracking-tight">
          {winner.name}
        </h3>
      </div>

      {/*
        O grupo rotulado embrulha a lista em vez de substituir o papel dela: é o
        que permite ao teste afirmar a AUSÊNCIA de barra e porcentagem por nome,
        sem custar ao `dl` a própria semântica de termo e definição.
      */}
      <div role="group" aria-label="Probabilidade de vitória">
        <dl className="grid gap-5">
          <ProbabilityBar
            name={fighterA.name}
            value={probA}
            corner="red"
            isWinner={winnerIsA}
          />
          <ProbabilityBar
            name={fighterB.name}
            value={probB}
            corner="blue"
            isWinner={!winnerIsA}
          />
        </dl>
      </div>
    </div>
  );
}

/**
 * Uma linha da probabilidade: nome, barra proporcional e percentual. A barra usa
 * a cor do canto; o vencedor fica em destaque. O texto carrega a informação, a
 * barra apenas a reforça — por isso ela é `aria-hidden`.
 */
function ProbabilityBar({
  name,
  value,
  corner,
  isWinner,
}: {
  name: string;
  value: number;
  corner: "red" | "blue";
  isWinner: boolean;
}) {
  const width = clampProbability(value) * 100;

  return (
    <div className="grid grid-cols-[1fr_3.5rem] items-center gap-x-4 gap-y-2">
      <dt className="min-w-0 truncate font-display text-sm font-semibold uppercase tracking-wide">
        {name}
      </dt>
      <dd
        className={
          isWinner
            ? "text-right font-mono text-sm font-bold tabular-nums text-belt-gold"
            : "text-right font-mono text-sm font-bold tabular-nums text-muted-foreground"
        }
      >
        {formatProbability(value)}
      </dd>
      <div
        aria-hidden="true"
        className="col-span-2 h-2.5 w-full overflow-hidden rounded-full border border-border bg-card"
      >
        <span
          className={
            corner === "red"
              ? "block h-full bg-corner-red"
              : "block h-full bg-corner-blue"
          }
          style={{ width: `${String(width)}%` }}
        />
      </div>
    </div>
  );
}
