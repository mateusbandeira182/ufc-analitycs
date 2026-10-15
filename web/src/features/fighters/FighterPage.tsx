import type { ReactNode } from "react";

import { AlertTriangle, UserX } from "lucide-react";
import { useParams } from "react-router";

import { ApiError } from "@/api/client";
import { Avatar } from "@/components/ui/avatar";
import { Skeleton } from "@/components/ui/skeleton";
import { FighterBoutHistory } from "@/features/fighters/FighterBoutHistory";
import { FighterCareerStats } from "@/features/fighters/FighterCareerStats";
import { FighterCurrentForm } from "@/features/fighters/FighterCurrentForm";
import { FighterFinishBreakdown } from "@/features/fighters/FighterFinishBreakdown";
import { FighterRecord } from "@/features/fighters/FighterRecord";
import { FighterStatsExcerpt } from "@/features/fighters/FighterStatsExcerpt";
import { FighterStrikingProfile } from "@/features/fighters/FighterStrikingProfile";
import { useFighter } from "@/features/fighters/useFighter";
import { useFighterStats } from "@/features/fighters/useFighterStats";

/**
 * Rota `/fighters/:id`: a página única do lutador. Absorveu a tela de
 * estatísticas que vivia em `/fighters/:id/stats` (RF-24) — a rota antiga
 * sobrevive só como redirecionamento.
 *
 * As três famílias de estatística convivem aqui em blocos **rotulados e
 * separados**, e essa separação é o conteúdo: CARREIRA é o agregado de todo o
 * acervo, FORMA ATUAL é o recorte as-of das três últimas lutas (o mesmo que o
 * modelo enxerga) e o RECORTE é o corte que o leitor escolheu no navegador.
 * Misturá-las é o defeito que a RF-24 existe para evitar.
 */
export function FighterPage() {
  const { id } = useParams();
  const fighterId = Number(id);
  const { data, isPending, isError, error } = useFighter(fighterId);

  // Id de rota inválido (ex.: `/fighters/abc`): tratado como não encontrado, sem
  // disparar request (o hook fica desabilitado). Consistente com /events e /bouts.
  if (Number.isNaN(fighterId)) {
    return <NotFoundScreen />;
  }

  if (isPending) {
    return <FighterPageSkeleton />;
  }

  if (isError) {
    const notFound = error instanceof ApiError && error.status === 404;
    return notFound ? (
      <NotFoundScreen />
    ) : (
      <StatusScreen
        icon={<AlertTriangle className="size-6 text-primary" />}
        title="Não foi possível carregar o lutador"
        description="A API não respondeu como esperado. Tente novamente em instantes."
      />
    );
  }

  return (
    <section className="mx-auto w-full max-w-5xl px-4 py-10">
      <header className="mb-6 flex items-center gap-4">
        {/* Corpo inteiro quando houver, retrato como segunda opção, iniciais
            sempre: só 794 dos 2.754 lutadores têm imagem, e o fallback é o caso
            comum. O nome acessível vem do `<h1>` ao lado. */}
        <Avatar
          name={data.name}
          src={data.body_image_url ?? data.headshot_url}
          className="size-20 sm:size-28"
        />
        <div className="min-w-0">
          <h1 className="font-display text-4xl font-bold uppercase tracking-tight">
            {data.name}
          </h1>
          {data.nickname ? (
            <p className="mt-1 text-lg italic text-muted-foreground">
              &ldquo;{data.nickname}&rdquo;
            </p>
          ) : null}
        </div>
      </header>

      <FighterRecord fighter={data} />

      <div className="mt-10 grid gap-10">
        {/* Lado a lado: a comparação entre o acervo inteiro e as três últimas
            lutas é a leitura que a página existe para oferecer. */}
        <div className="grid gap-10 lg:grid-cols-2">
          <FighterCareerStats fighterId={fighterId} />
          <FighterCurrentForm fighterId={fighterId} />
        </div>

        <CareerFinish fighterId={fighterId} />
        <FighterStrikingProfile fighterId={fighterId} />
        <FighterStatsExcerpt fighterId={fighterId} />

        <div>
          <h2 className="mb-4 font-display text-xl font-bold uppercase tracking-wide">
            Histórico no UFC
          </h2>
          <FighterBoutHistory fighterId={fighterId} />
        </div>
      </div>
    </section>
  );
}

/**
 * "Como vence" de carreira: as vitórias por método que o servidor agrega sobre
 * todo o acervo. Local à página porque é só o arranjo de dado para o componente
 * compartilhado — o desenho inteiro vive no `FighterFinishBreakdown`.
 *
 * Carregamento e erro não aparecem aqui: a seção CARREIRA já consome a mesma
 * query e já reporta os dois, e repetir a mensagem a duplicaria na tela.
 */
function CareerFinish({ fighterId }: { fighterId: number }) {
  const { data } = useFighterStats(fighterId);

  if (data === undefined) {
    return null;
  }

  return (
    <FighterFinishBreakdown
      title="Como vence"
      headingLevel={2}
      winsByMethod={data.wins_by_method}
    />
  );
}

function FighterPageSkeleton() {
  return (
    <section
      className="mx-auto w-full max-w-5xl px-4 py-10"
      aria-busy="true"
      aria-label="Carregando lutador"
    >
      <Skeleton className="h-10 w-64" />
      <Skeleton className="mt-6 h-32 w-full" />
    </section>
  );
}

function NotFoundScreen() {
  return (
    <StatusScreen
      icon={<UserX className="size-6 text-muted-foreground" />}
      title="Lutador não encontrado"
      description="Não há nenhum lutador com esse identificador no acervo."
    />
  );
}

function StatusScreen({
  icon,
  title,
  description,
}: {
  icon: ReactNode;
  title: string;
  description: string;
}) {
  return (
    <section className="mx-auto w-full max-w-5xl px-4 py-10">
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
    </section>
  );
}
