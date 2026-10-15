import type { ReactNode } from "react";

import { AlertTriangle, ServerCrash, Swords, UserX, Users } from "lucide-react";
import { useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import type { FighterOut } from "@/api/schema";
import { Skeleton } from "@/components/ui/skeleton";
import { AttributeComparison } from "@/features/confronto/AttributeComparison";
import { BlockInfluenceBars } from "@/features/confronto/BlockInfluenceBars";
import { CommonOpponents } from "@/features/confronto/CommonOpponents";
import { DirectBouts } from "@/features/confronto/DirectBouts";
import { PredictionSection } from "@/features/confronto/PredictionSection";
import { useHeadToHead } from "@/features/confronto/useHeadToHead";
import { useMatchupPrediction } from "@/features/confronto/useMatchupPrediction";
import { FighterSelector } from "@/features/fighters/FighterSelector";
import { useFighter } from "@/features/fighters/useFighter";

/** Lê um id de lutador da URL; nulo quando ausente ou não é inteiro positivo. */
function parseId(raw: string | null): number | null {
  if (!raw) {
    return null;
  }
  const value = Number(raw);
  return Number.isInteger(value) && value > 0 ? value : null;
}

/**
 * Rota `/confronto?a=&b=`: a página única de confronto. Absorveu
 * `/head-to-head` e `/matchup` (RF-23), que sobrevivem só como
 * redirecionamento.
 *
 * A URL é a fonte da verdade da dupla — compartilhável, recarregável e
 * alcançável por um clique vindo do card de um evento. Não há botão de
 * submissão: a previsão carrega com a dupla.
 *
 * A guarda `a === b` é client-side e **anterior à rede**: nenhuma das quatro
 * queries dispara, e por isso o 422 do endpoint é inalcançável aqui.
 */
export function ConfrontoPage() {
  const [params, setParams] = useSearchParams();
  const a = parseId(params.get("a"));
  const b = parseId(params.get("b"));
  const sameFighter = a !== null && a === b;

  function setSide(side: "a" | "b", fighterId: number) {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set(side, String(fighterId));
      return next;
    });
  }

  return (
    <section className="mx-auto w-full max-w-5xl px-4 py-10">
      <header className="mb-8">
        <p className="font-display text-xs font-medium uppercase tracking-[0.3em] text-primary">
          Dois atletas, um octógono
        </p>
        <h1 className="font-display text-4xl font-bold uppercase tracking-tight">
          Confronto
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Escolha dois atletas e veja o palpite do modelo, o que moveu a agulha,
          os atributos lado a lado e o que cada um fez contra quem os dois
          enfrentaram.
        </p>
      </header>

      <div className="grid gap-4 sm:grid-cols-2">
        <FighterSelector
          label="Lutador A"
          selectedId={a}
          onSelect={(id) => {
            setSide("a", id);
          }}
        />
        <FighterSelector
          label="Lutador B"
          selectedId={b}
          onSelect={(id) => {
            setSide("b", id);
          }}
        />
      </div>

      <div className="mt-10">
        {sameFighter ? (
          <StatusPanel
            icon={<Swords className="size-6 text-primary" />}
            title="Escolha dois atletas diferentes"
            description="O confronto compara dois atletas distintos. Troque um dos lados para continuar."
          />
        ) : a === null || b === null ? (
          <StatusPanel
            icon={<Users className="size-6 text-muted-foreground" />}
            title="Escolha os dois atletas"
            description="Busque um atleta em cada campo acima para montar o confronto."
          />
        ) : (
          <ConfrontoResult a={a} b={b} />
        )}
      </div>
    </section>
  );
}

/**
 * Orquestra as quatro queries do confronto. Nenhuma depende da outra, então elas
 * correm em paralelo — não serializar.
 *
 * As três leituras (detalhe de cada lado + confronto direto) são o que a página
 * precisa para existir: enquanto carregam, o esqueleto cobre tudo. A previsão é
 * tratada **em separado**, dentro da própria seção: ela falhar (503/500) ou vir
 * indisponível não pode derrubar adversários em comum nem confronto direto, que
 * não dependem do modelo.
 */
function ConfrontoResult({ a, b }: { a: number; b: number }) {
  const fighterA = useFighter(a);
  const fighterB = useFighter(b);
  const headToHead = useHeadToHead(a, b);

  if (fighterA.isPending || fighterB.isPending || headToHead.isPending) {
    return <ConfrontoSkeleton />;
  }

  if (fighterA.isError || fighterB.isError || headToHead.isError) {
    const notFound = [fighterA.error, fighterB.error, headToHead.error].some(
      (error) => error instanceof ApiError && error.status === 404,
    );
    return notFound ? (
      <StatusPanel
        icon={<UserX className="size-6 text-muted-foreground" />}
        title="Lutador não encontrado"
        description="Um dos identificadores da URL não corresponde a nenhum lutador do acervo."
      />
    ) : (
      <StatusPanel
        icon={<AlertTriangle className="size-6 text-primary" />}
        title="Não foi possível carregar o confronto"
        description="A API não respondeu como esperado. Tente novamente em instantes."
      />
    );
  }

  return (
    <div className="grid gap-12">
      <Prediction fighterA={fighterA.data} fighterB={fighterB.data} />
      <CommonOpponents
        opponents={headToHead.data.common_opponents}
        sideA={fighterA.data}
        sideB={fighterB.data}
      />
      <DirectBouts
        bouts={headToHead.data.bouts}
        nameA={fighterA.data.name}
        nameB={fighterB.data.name}
      />
    </div>
  );
}

/**
 * A previsão e a explicabilidade que dela depende. As seções de blocos e de
 * atributos vêm do mesmo payload e não aparecem quando ele não as traz — no
 * estado indisponível o contrato manda listas **vazias** (DP-05e), e uma casca
 * vazia na tela diria menos que nada.
 */
function Prediction({
  fighterA,
  fighterB,
}: {
  fighterA: FighterOut;
  fighterB: FighterOut;
}) {
  const { data, isPending, isError, error } = useMatchupPrediction(
    fighterA.id,
    fighterB.id,
  );

  if (isPending) {
    return <PredictionSkeleton />;
  }

  if (isError) {
    return <PredictionError error={error} />;
  }

  return (
    <div className="grid gap-12">
      <PredictionSection
        prediction={data}
        fighterA={fighterA}
        fighterB={fighterB}
      />
      <BlockInfluenceBars
        blocks={data.blocks}
        nameA={fighterA.name}
        nameB={fighterB.name}
      />
      <AttributeComparison
        attributes={data.attributes}
        nameA={fighterA.name}
        nameB={fighterB.name}
      />
    </div>
  );
}

/**
 * Falha da previsão, por status. O 422 de lutadores iguais **não** é tratado: a
 * guarda client-side é anterior à rede, então ele é inalcançável aqui e um ramo
 * para ele seria caminho morto.
 */
function PredictionError({ error }: { error: unknown }) {
  const status = error instanceof ApiError ? error.status : null;

  if (status === 503) {
    return (
      <StatusPanel
        icon={<ServerCrash className="size-6 text-primary" />}
        title="Modelo indisponível no momento"
        description="O modelo preditivo não está disponível agora. As outras seções do confronto seguem abaixo."
      />
    );
  }
  return (
    <StatusPanel
      icon={<AlertTriangle className="size-6 text-primary" />}
      title="Não foi possível gerar a previsão"
      description="A API não respondeu como esperado. As outras seções do confronto seguem abaixo."
    />
  );
}

function ConfrontoSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Carregando confronto"
      className="grid gap-6"
    >
      <Skeleton className="h-48 w-full" />
      <Skeleton className="h-40 w-full" />
    </div>
  );
}

function PredictionSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Calculando previsão"
      className="grid gap-6"
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
