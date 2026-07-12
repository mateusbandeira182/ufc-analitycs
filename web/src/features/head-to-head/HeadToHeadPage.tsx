import type { ReactNode } from "react";

import { AlertTriangle, Swords, UserX, Users } from "lucide-react";
import { useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import { Skeleton } from "@/components/ui/skeleton";
import { useFighter } from "@/features/fighters/useFighter";
import { FighterSelector } from "@/features/head-to-head/FighterSelector";
import { HeadToHeadComparison } from "@/features/head-to-head/HeadToHeadComparison";
import { useHeadToHead } from "@/features/head-to-head/useHeadToHead";

/** Lê um id de lutador da URL; nulo quando ausente ou não é inteiro positivo. */
function parseId(raw: string | null): number | null {
  if (!raw) {
    return null;
  }
  const value = Number(raw);
  return Number.isInteger(value) && value > 0 ? value : null;
}

/**
 * Rota `/head-to-head?a=&b=`: a URL é a fonte da verdade dos dois lutadores
 * (compartilhável e recarregável). Orquestra os dois seletores e delega a
 * comparação, tratando os casos incompleto e `a == b` sem ir à rede.
 */
export function HeadToHeadPage() {
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
          Confronto direto
        </p>
        <h1 className="font-display text-4xl font-bold uppercase tracking-tight">
          Head-to-head
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Escolha dois atletas e compare cartel, atributos e o retrospecto do
          confronto direto.
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
            title="Selecione dois lutadores distintos"
            description="O confronto compara dois atletas diferentes. Troque um dos lados para continuar."
          />
        ) : a === null || b === null ? (
          <StatusPanel
            icon={<Users className="size-6 text-muted-foreground" />}
            title="Escolha os dois lutadores"
            description="Busque um atleta em cada campo acima para ver a comparação lado a lado."
          />
        ) : (
          <HeadToHeadResult a={a} b={b} />
        )}
      </div>
    </section>
  );
}

/**
 * Orquestra as três queries do confronto (detalhe de cada lutador + confronto
 * direto). Só é montado com `a` e `b` definidos e distintos, então `useFighter`
 * recebe sempre ids válidos. Trata carregamento, lutador inexistente (404) e
 * erro genérico antes de delegar à comparação.
 */
function HeadToHeadResult({ a, b }: { a: number; b: number }) {
  const fighterA = useFighter(a);
  const fighterB = useFighter(b);
  const headToHead = useHeadToHead(a, b);

  if (fighterA.isPending || fighterB.isPending || headToHead.isPending) {
    return <ComparisonSkeleton />;
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
    <HeadToHeadComparison
      fighterA={fighterA.data}
      fighterB={fighterB.data}
      bouts={headToHead.data.bouts}
    />
  );
}

function ComparisonSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Carregando confronto"
      className="grid gap-4 lg:grid-cols-2"
    >
      <Skeleton className="h-40 w-full" />
      <Skeleton className="h-40 w-full" />
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
