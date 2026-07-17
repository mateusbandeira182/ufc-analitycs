import { AlertTriangle, ServerCrash, Swords, Trophy } from "lucide-react";
import { useState, type ReactNode } from "react";

import { ApiError } from "@/api/client";
import type { MatchupPredictionOut } from "@/api/schema";
import { Skeleton } from "@/components/ui/skeleton";
import { FighterSelector } from "@/features/head-to-head/FighterSelector";
import { clampProbability, formatProbability } from "@/features/matchup/format";
import { useMatchupPrediction } from "@/features/matchup/useMatchupPrediction";

/** Par de lutadores efetivamente submetido ao modelo (só muda ao clicar em "Prever"). */
interface Matchup {
  a: number;
  b: number;
}

/**
 * Rota `/matchup`: escolhe dois lutadores e pede ao modelo preditivo um palpite
 * neutro de canto. A seleção vive em estado local; o palpite só é disparado ao
 * clicar em "Prever" (o par submetido habilita a query). A ordem A/B não muda o
 * resultado — o backend neutraliza a vantagem de canto.
 */
export function MatchupPage() {
  const [a, setA] = useState<number | null>(null);
  const [b, setB] = useState<number | null>(null);
  const [matchup, setMatchup] = useState<Matchup | null>(null);

  const canPredict = a !== null && b !== null;

  function predict() {
    if (a !== null && b !== null) {
      setMatchup({ a, b });
    }
  }

  return (
    <section className="mx-auto w-full max-w-5xl px-4 py-10">
      <header className="mb-8">
        <p className="font-display text-xs font-medium uppercase tracking-[0.3em] text-primary">
          Palpite do modelo
        </p>
        <h1 className="font-display text-4xl font-bold uppercase tracking-tight">
          Prevê essa luta
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Escolha dois atletas e veja quem o modelo aponta como favorito, com a
          probabilidade de vitória de cada lado.
        </p>
      </header>

      <div className="grid gap-4 sm:grid-cols-2">
        <FighterSelector label="Lutador A" selectedId={a} onSelect={setA} />
        <FighterSelector label="Lutador B" selectedId={b} onSelect={setB} />
      </div>

      <div className="mt-6">
        <button
          type="button"
          disabled={!canPredict}
          onClick={predict}
          className="inline-flex items-center gap-2 rounded-md bg-primary px-6 py-2.5 font-display text-sm font-bold uppercase tracking-wide text-primary-foreground transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Swords aria-hidden="true" className="size-4" />
          Prever
        </button>
      </div>

      <div className="mt-10">
        {matchup === null ? (
          <StatusPanel
            icon={<Swords className="size-6 text-muted-foreground" />}
            title="Monte o confronto"
            description="Selecione um atleta em cada campo e toque em Prever para ver o palpite do modelo."
          />
        ) : (
          <MatchupResult a={matchup.a} b={matchup.b} />
        )}
      </div>
    </section>
  );
}

/**
 * Consome o palpite do modelo para o par submetido e trata os três estados do
 * server-state. Os erros do endpoint viram mensagens amigáveis por status
 * (422 lutadores iguais, 404 inexistente, 503 modelo fora, genérico para o resto)
 * — nunca uma tela quebrada.
 */
function MatchupResult({ a, b }: { a: number; b: number }) {
  const { data, isPending, isError, error } = useMatchupPrediction(a, b);

  if (isPending) {
    return <ResultSkeleton />;
  }

  if (isError) {
    return <ErrorPanel error={error} />;
  }

  return <PredictionCard prediction={data} />;
}

/** Traduz o erro do endpoint em um painel amigável, por status HTTP. */
function ErrorPanel({ error }: { error: unknown }) {
  const status = error instanceof ApiError ? error.status : null;

  if (status === 422) {
    return (
      <StatusPanel
        icon={<Swords className="size-6 text-primary" />}
        title="Escolha dois lutadores diferentes"
        description="O palpite compara dois atletas distintos. Troque um dos lados para continuar."
      />
    );
  }
  if (status === 404) {
    return (
      <StatusPanel
        icon={<AlertTriangle className="size-6 text-muted-foreground" />}
        title="Lutador não encontrado"
        description="Um dos atletas escolhidos não corresponde a nenhum registro do acervo."
      />
    );
  }
  if (status === 503) {
    return (
      <StatusPanel
        icon={<ServerCrash className="size-6 text-primary" />}
        title="Modelo indisponível no momento"
        description="O modelo preditivo não está disponível agora. Tente novamente em instantes."
      />
    );
  }
  return (
    <StatusPanel
      icon={<AlertTriangle className="size-6 text-primary" />}
      title="Não foi possível gerar o palpite"
      description="A API não respondeu como esperado — pode faltar histórico para um dos atletas. Tente outro confronto."
    />
  );
}

/**
 * Resultado do palpite: o vencedor previsto em destaque e as duas barras de
 * probabilidade complementares. A barra do vencedor é realçada. As probabilidades
 * são saneadas (nunca NaN/Infinity) antes de virar texto ou largura.
 */
function PredictionCard({ prediction }: { prediction: MatchupPredictionOut }) {
  const { fighter_a, fighter_b, prob_a_wins, prob_b_wins } = prediction;
  const winnerIsA = prediction.predicted_winner_id === fighter_a.id;
  const winnerName = winnerIsA ? fighter_a.name : fighter_b.name;

  return (
    <div className="grid gap-8">
      <div
        role="status"
        aria-label="Vencedor previsto"
        className="flex flex-col items-center gap-3 rounded-lg border border-primary/40 bg-card px-6 py-10 text-center"
      >
        <Trophy aria-hidden="true" className="size-8 text-belt-gold" />
        <p className="font-display text-xs font-medium uppercase tracking-[0.3em] text-primary">
          Vencedor previsto
        </p>
        <h2 className="font-display text-3xl font-bold uppercase tracking-tight">
          {winnerName}
        </h2>
      </div>

      <dl className="grid gap-5">
        <ProbabilityBar
          name={fighter_a.name}
          value={prob_a_wins}
          isWinner={winnerIsA}
        />
        <ProbabilityBar
          name={fighter_b.name}
          value={prob_b_wins}
          isWinner={!winnerIsA}
        />
      </dl>
    </div>
  );
}

/**
 * Uma linha da probabilidade: nome do lutador, a barra proporcional e o
 * percentual. A barra do vencedor usa o dourado; a do perdedor, tom neutro. O
 * texto (percentual) carrega a informação; a barra apenas a reforça.
 */
function ProbabilityBar({
  name,
  value,
  isWinner,
}: {
  name: string;
  value: number;
  isWinner: boolean;
}) {
  const width = clampProbability(value) * 100;

  return (
    <div className="grid grid-cols-[1fr_3.5rem] items-center gap-x-4 gap-y-2">
      <dt className="font-display text-sm font-semibold uppercase tracking-wide">
        {name}
      </dt>
      <dd className={cnPercent(isWinner)}>{formatProbability(value)}</dd>
      <div
        aria-hidden="true"
        className="col-span-2 h-2.5 w-full overflow-hidden rounded-full border border-border bg-card"
      >
        <span
          className={
            isWinner
              ? "block h-full bg-belt-gold"
              : "block h-full bg-muted-foreground/40"
          }
          style={{ width: `${String(width)}%` }}
        />
      </div>
    </div>
  );
}

/** Classe do percentual: realça o vencedor em dourado, o perdedor em tom neutro. */
function cnPercent(isWinner: boolean): string {
  return isWinner
    ? "text-right font-mono text-sm font-bold tabular-nums text-belt-gold"
    : "text-right font-mono text-sm font-bold tabular-nums text-muted-foreground";
}

function ResultSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Calculando palpite"
      className="grid gap-8"
    >
      <Skeleton className="h-40 w-full" />
      <Skeleton className="h-20 w-full" />
    </div>
  );
}

function StatusPanel({
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
