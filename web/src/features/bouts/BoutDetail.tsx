import type { ReactNode } from "react";

import {
  AlertTriangle,
  CalendarClock,
  CalendarDays,
  HelpCircle,
  Swords,
  Trophy,
} from "lucide-react";
import { Fragment } from "react";
import { Link, useParams } from "react-router";

import { ApiError } from "@/api/client";
import type { BoutDetailOut, BoutFighterStatsOut } from "@/api/schema";
import { Skeleton } from "@/components/ui/skeleton";
import { BoutStats } from "@/features/bouts/BoutStats";
import { resolveResult, formatDuration } from "@/features/bouts/format";
import { useBout } from "@/features/bouts/useBout";
import { formatIsoDate, formatMethod } from "@/lib/format";

/**
 * Rota `/bouts/:id`: orquestra o detalhe da luta. Lê o id da URL, trata id
 * inválido e inexistente (404) como não encontrado, delega o server-state ao
 * `useBout` e monta cabeçalho (evento + resultado) + a tabela comparativa de stats.
 *
 * Com `bouts.status` no acervo, a rota passou a ser alcançável para uma luta
 * **agendada**, e aí a página é outra: o confronto, o contexto do card e o aviso
 * de que a luta ainda não aconteceu — **sem** box-score. `BoutStats` com as
 * stats nulas renderizaria uma tabela inteira de traços, que é a mesma mentira
 * que o zero. Quem decide o modo é o `status` (RF-31 / D-14), nunca a ausência
 * de `method`.
 */
export function BoutDetail() {
  const { id } = useParams();
  const boutId = Number(id);
  const { data, isPending, isError, error } = useBout(boutId);

  // Id de rota inválido (ex.: `/bouts/abc`): tratado como não encontrado, sem
  // disparar request (o hook fica desabilitado).
  if (Number.isNaN(boutId)) {
    return <NotFoundScreen />;
  }

  if (isPending) {
    return <BoutDetailSkeleton />;
  }

  if (isError) {
    const notFound = error instanceof ApiError && error.status === 404;
    return notFound ? (
      <NotFoundScreen />
    ) : (
      <StatusScreen
        icon={<AlertTriangle className="size-6 text-primary" />}
        title="Não foi possível carregar a luta"
        description="A API não respondeu como esperado. Tente novamente em instantes."
      />
    );
  }

  if (data.status === "scheduled") {
    return (
      <section className="mx-auto w-full max-w-3xl px-4 py-10">
        <ScheduledBout bout={data} />
      </section>
    );
  }

  return (
    <section className="mx-auto w-full max-w-3xl px-4 py-10">
      <BoutHeader bout={data} />
      <div className="mt-10">
        <BoutStats fighters={data.fighters} winnerId={data.winner_id} />
      </div>
    </section>
  );
}

/** Evento de origem com data: o mesmo cabeçalho nos dois modos da página. */
function EventLine({ bout }: { bout: BoutDetailOut }) {
  return (
    <>
      <Link
        to={`/events/${String(bout.event.id)}`}
        className="inline-flex items-center gap-1.5 font-display text-xs font-medium uppercase tracking-[0.3em] text-primary hover:text-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <CalendarDays aria-hidden="true" className="size-3.5" />
        {bout.event.name}
      </Link>
      <p className="mt-1 text-xs text-muted-foreground">
        {formatIsoDate(bout.event.date)}
      </p>
    </>
  );
}

/**
 * A luta que ainda não aconteceu. Declara a ausência em vez de a preencher: o
 * aviso, o confronto e o contexto do card que **existir** — nenhum número, e
 * nem o traço, porque ele responde a outra pergunta.
 */
function ScheduledBout({ bout }: { bout: BoutDetailOut }) {
  const ordered = [...bout.fighters].sort((a, b) =>
    a.corner === b.corner ? 0 : a.corner === "red" ? -1 : 1,
  );
  const red = bout.fighters.find((fighter) => fighter.corner === "red");
  const blue = bout.fighters.find((fighter) => fighter.corner === "blue");

  return (
    <>
      <header>
        <EventLine bout={bout} />

        <div className="mt-6 flex items-start gap-3 rounded-lg border border-dashed border-border bg-card/50 px-6 py-5">
          <CalendarClock
            aria-hidden="true"
            className="mt-0.5 size-6 shrink-0 text-belt-gold"
          />
          <div>
            <p className="font-display text-xl font-bold uppercase leading-tight tracking-tight">
              Esta luta ainda não aconteceu
            </p>
            <p className="mt-1 text-sm text-muted-foreground">
              Quando ela acontecer, o box-score round a round aparece aqui. Até
              lá, só existe o confronto marcado.
            </p>
          </div>
        </div>
      </header>

      <div className="mt-8 grid gap-8">
        <section aria-labelledby="confronto-marcado">
          <h2
            id="confronto-marcado"
            className="mb-4 font-display text-xl font-bold uppercase tracking-wide"
          >
            Confronto marcado
          </h2>
          <div className="grid grid-cols-[1fr_auto_1fr] items-stretch overflow-hidden rounded-lg border border-border bg-card">
            {ordered.map((fighter, index) => (
              <Fragment key={fighter.fighter_id}>
                {index > 0 ? (
                  <div className="flex items-center justify-center px-2">
                    <span className="font-display text-xs font-bold uppercase tracking-widest text-primary">
                      vs
                    </span>
                  </div>
                ) : null}
                <ScheduledCorner fighter={fighter} />
              </Fragment>
            ))}
          </div>
          {red && blue ? (
            <Link
              to={`/confronto?a=${String(red.fighter_id)}&b=${String(blue.fighter_id)}`}
              className="mt-3 inline-flex items-center gap-1 text-xs font-medium uppercase tracking-wide text-muted-foreground transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <HelpCircle aria-hidden="true" className="size-3.5" />
              Por que? Ver o palpite do modelo
            </Link>
          ) : null}
        </section>

        <CardContext bout={bout} />
      </div>
    </>
  );
}

/** Um canto do confronto marcado: faixa da cor, rótulo e o nome com link. */
function ScheduledCorner({ fighter }: { fighter: BoutFighterStatsOut }) {
  const cornerAccent =
    fighter.corner === "red" ? "bg-corner-red" : "bg-corner-blue";

  return (
    <div className="relative flex flex-col items-center gap-1 px-4 pb-5 pt-6 text-center">
      <span
        aria-hidden="true"
        className={`absolute inset-x-0 top-0 h-1 ${cornerAccent}`}
      />
      <span className="text-[0.6rem] font-medium uppercase tracking-[0.2em] text-muted-foreground">
        {fighter.corner === "red" ? "Canto vermelho" : "Canto azul"}
      </span>
      <Link
        to={`/fighters/${String(fighter.fighter_id)}`}
        className="font-display text-lg font-semibold uppercase leading-tight tracking-wide hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {fighter.name}
      </Link>
    </div>
  );
}

/**
 * O contexto do card que o payload **traz**. Cada termo é condicional: campo
 * ausente não aparece, nem como traço mudo — o enriquecimento da Cito só cobre
 * evento recente, e `fill_bout_context` tem o `bout_order` como porteiro.
 *
 * `bout_order` fica de fora de propósito: dizer "luta principal" exigiria o
 * mínimo do evento inteiro, que a página de uma luta não tem, e renderizá-lo
 * como número reintroduziria a assunção de faixa densa que a RF-27 proíbe.
 */
function CardContext({ bout }: { bout: BoutDetailOut }) {
  const termos: { term: string; value: string }[] = [];
  if (bout.card_section !== null) {
    termos.push({ term: "Seção do card", value: bout.card_section });
  }
  if (bout.weight_class !== null) {
    termos.push({ term: "Divisão", value: bout.weight_class });
  }
  if (bout.scheduled_rounds !== null) {
    termos.push({
      term: "Formato",
      value: `${String(bout.scheduled_rounds)} rounds`,
    });
  }
  if (bout.title_bout === true) {
    termos.push({ term: "Disputa", value: "Luta de título" });
  }

  if (termos.length === 0) {
    return null;
  }

  return (
    <section
      role="group"
      aria-label="Contexto do card"
      className="rounded-lg border border-border bg-card px-6 py-5"
    >
      <h2 className="font-display text-xs font-semibold uppercase tracking-[0.25em] text-muted-foreground">
        Contexto do card
      </h2>
      <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-3">
        {termos.map(({ term, value }) => (
          <div key={term}>
            <dt className="text-[0.6rem] uppercase tracking-widest text-muted-foreground/60">
              {term}
            </dt>
            <dd className="mt-0.5 text-sm text-foreground">{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function BoutHeader({ bout }: { bout: BoutDetailOut }) {
  const result = resolveResult(bout);

  return (
    <header>
      <EventLine bout={bout} />

      <div
        role="group"
        aria-label="Resultado da luta"
        className="mt-6 rounded-lg border border-border bg-card px-6 py-5"
      >
        <Verdict result={result} />

        <dl className="mt-4 flex flex-wrap gap-x-6 gap-y-2 font-mono text-xs uppercase tracking-widest text-muted-foreground">
          <ResultMeta term="Método" value={formatMethod(bout.method)} />
          {bout.round !== null ? (
            <ResultMeta term="Round" value={`Round ${String(bout.round)}`} />
          ) : null}
          {bout.ending_time_seconds !== null ? (
            <ResultMeta
              term="Tempo"
              value={formatDuration(bout.ending_time_seconds)}
            />
          ) : null}
          {bout.weight_class !== null ? (
            <ResultMeta term="Categoria" value={bout.weight_class} />
          ) : null}
        </dl>
      </div>
    </header>
  );
}

function Verdict({ result }: { result: ReturnType<typeof resolveResult> }) {
  if (result.kind === "winner") {
    return (
      <div className="flex items-center gap-3">
        <Trophy aria-hidden="true" className="size-6 shrink-0 text-belt-gold" />
        <div>
          <p className="text-[0.65rem] font-bold uppercase tracking-[0.3em] text-belt-gold">
            Vencedor
          </p>
          <p className="font-display text-3xl font-bold uppercase leading-tight tracking-tight">
            {result.name}
          </p>
        </div>
      </div>
    );
  }

  // `scheduled` não chega aqui: a luta agendada tem a sua própria página
  // (`ScheduledBout`), que é o que a CA-30 pede. O ramo permanece no `switch` de
  // rótulos porque `BoutResult` o declara, e a tela degrada em vez de estourar
  // se um dia um caminho novo trouxer uma agendada por este lado.
  const label =
    result.kind === "draw"
      ? "Empate"
      : result.kind === "scheduled"
        ? "Luta agendada"
        : "Sem vencedor";
  return (
    <div className="flex items-center gap-3">
      <Swords
        aria-hidden="true"
        className="size-6 shrink-0 text-muted-foreground"
      />
      <p className="font-display text-3xl font-bold uppercase leading-tight tracking-tight text-muted-foreground">
        {label}
      </p>
    </div>
  );
}

function ResultMeta({ term, value }: { term: string; value: string }) {
  return (
    <div>
      <dt className="text-[0.6rem] text-muted-foreground/60">{term}</dt>
      <dd className="mt-0.5 text-sm normal-case tracking-normal text-foreground">
        {value}
      </dd>
    </div>
  );
}

function BoutDetailSkeleton() {
  return (
    <section
      className="mx-auto w-full max-w-3xl px-4 py-10"
      aria-busy="true"
      aria-label="Carregando luta"
    >
      <Skeleton className="h-4 w-40" />
      <Skeleton className="mt-6 h-28 w-full" />
      <Skeleton className="mt-8 h-64 w-full" />
    </section>
  );
}

function NotFoundScreen() {
  return (
    <StatusScreen
      icon={<Swords className="size-6 text-muted-foreground" />}
      title="Luta não encontrada"
      description="Não há nenhuma luta com esse identificador no acervo."
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
    <section className="mx-auto w-full max-w-3xl px-4 py-10">
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
