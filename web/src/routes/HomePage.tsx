import { ArrowRight, CalendarDays, MapPin, Trophy } from "lucide-react";
import { Link } from "react-router";

import { ApiError } from "@/api/client";
import type {
  BoutCardFighterOut,
  BoutCardOut,
  EventDetailOut,
} from "@/api/schema";
import { Avatar } from "@/components/ui/avatar";
import { Skeleton } from "@/components/ui/skeleton";
import { EventHighlightsBlock } from "@/features/events/EventHighlightsBlock";
import { EventList } from "@/features/events/EventList";
import { EventSummaryBlock } from "@/features/events/EventSummaryBlock";
import { useEvents } from "@/features/events/useEvents";
import { useLatestEvent } from "@/features/events/useLatestEvent";
import { formatEndingTime, formatIsoDate, formatMethod } from "@/lib/format";

/** Quantos eventos por vir a Home mostra. A lista completa vive em `/events`. */
const HOME_UPCOMING_LIMIT = 5;

/**
 * Home (`/`): a porta de entrada abre no que é mais característico deste
 * acervo — o card que acabou de acontecer. A busca saiu do hero e virou
 * permanente no cabeçalho (`AppLayout`), porque procurar lutador é a ação mais
 * frequente e não pode depender de estar nesta página.
 *
 * Em cima, o último evento realizado: a luta principal como um confronto de
 * cantos, a contagem por método com o denominador e os destaques rotulados
 * "calculado, não oficial". Embaixo, os próximos eventos.
 */
export function HomePage() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-10 sm:py-14">
      <LatestEventSection />
      <UpcomingEventsSection />
    </div>
  );
}

/** O card do último evento realizado, com seus quatro estados observáveis. */
function LatestEventSection() {
  const { data, isPending, isError, error } = useLatestEvent();

  if (isPending) {
    return (
      <div aria-busy="true" aria-label="Carregando último evento">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="mt-4 h-40 w-full" />
        <Skeleton className="mt-3 h-20 w-full" />
      </div>
    );
  }

  if (isError) {
    // 404 não é falha: é o acervo dizendo que não tem evento realizado. A
    // distinção importa porque as duas situações pedem frases diferentes.
    const vazio = error instanceof ApiError && error.status === 404;
    return (
      <div
        role="status"
        className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/50 px-6 py-16 text-center"
      >
        <p className="font-display text-lg font-semibold uppercase tracking-wide">
          {vazio
            ? "Nenhum evento realizado no acervo"
            : "Não foi possível carregar o último evento"}
        </p>
        <p className="max-w-sm text-sm text-muted-foreground">
          {vazio
            ? "A ingestão histórica ainda não rodou. Os eventos por vir seguem abaixo."
            : "A API não respondeu como esperado. Tente novamente em instantes."}
        </p>
      </div>
    );
  }

  return (
    <section aria-labelledby="ultimo-evento" className="grid gap-8">
      <LatestEventHeader event={data} />
      {data.bouts[0] ? <MainEventFaceOff bout={data.bouts[0]} /> : null}
      <EventSummaryBlock summary={data.summary} />
      <EventHighlightsBlock highlights={data.highlights} />
    </section>
  );
}

function LatestEventHeader({ event }: { event: EventDetailOut }) {
  return (
    <header>
      <p className="font-display text-xs font-semibold uppercase tracking-[0.35em] text-primary">
        Último evento
      </p>
      <h2
        id="ultimo-evento"
        className="mt-2 font-display text-4xl font-bold uppercase leading-[0.95] tracking-tight sm:text-5xl"
      >
        {event.name}
      </h2>
      <p className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs uppercase tracking-wide">
        <span className="inline-flex items-center gap-1.5 font-mono tabular-nums text-belt-gold">
          <CalendarDays aria-hidden="true" className="size-3.5" />
          {formatIsoDate(event.date)}
        </span>
        {event.location ? (
          <span className="inline-flex items-center gap-1.5 text-muted-foreground">
            <MapPin aria-hidden="true" className="size-3.5" />
            {event.location}
          </span>
        ) : null}
      </p>
      <Link
        to={`/events/${String(event.id)}`}
        className="mt-4 inline-flex items-center gap-1.5 font-display text-xs font-semibold uppercase tracking-widest text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        Ver o card completo
        <ArrowRight aria-hidden="true" className="size-3.5" />
      </Link>
    </header>
  );
}

/**
 * O confronto da luta principal — o elemento de assinatura da Home. Os dois
 * retratos no recorte do octógono, maiores do que em qualquer outra tela, com
 * as cores de canto como moldura: é o vernáculo do esporte usado como estrutura.
 *
 * A principal é `bouts[0]`: o servidor é a autoridade da ordem (`bout_order asc
 * nulls_last, id asc`, e a principal é o **menor** `bout_order`), então a Home
 * não calcula mínimo nem reordena. Quando o card não tem `bout_order` nenhum —
 * o enriquecimento de contexto é irregular —, `bouts[0]` é a luta de menor id, e
 * é isso que o contrato oferece; a tela não inventa uma luta principal.
 */
function MainEventFaceOff({ bout }: { bout: BoutCardOut }) {
  // Vermelho primeiro, para exibição determinística — não confia na ordem do array.
  const ordered = [...bout.fighters].sort((a, b) =>
    a.corner === b.corner ? 0 : a.corner === "red" ? -1 : 1,
  );

  return (
    <section
      aria-label="Luta principal"
      className="overflow-hidden rounded-lg border border-border bg-card"
    >
      <div className="grid items-stretch gap-px bg-border sm:grid-cols-[1fr_auto_1fr]">
        {ordered.map((fighter, index) => (
          <FaceOffCorner
            key={fighter.fighter_id}
            fighter={fighter}
            isWinner={bout.winner_id === fighter.fighter_id}
            alignEnd={index > 0}
          />
        ))}
      </div>

      <p className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1 border-t border-border bg-secondary/40 px-4 py-3 font-mono text-[0.7rem] uppercase tracking-widest text-muted-foreground">
        <span className="text-foreground">{formatMethod(bout.method)}</span>
        <span aria-hidden="true">·</span>
        <span>{formatEndingTime(bout.round, bout.ending_time_seconds)}</span>
        {bout.weight_class ? (
          <>
            <span aria-hidden="true">·</span>
            <span>{bout.weight_class}</span>
          </>
        ) : null}
      </p>
    </section>
  );
}

function FaceOffCorner({
  fighter,
  isWinner,
  alignEnd,
}: {
  fighter: BoutCardFighterOut;
  isWinner: boolean;
  alignEnd: boolean;
}) {
  const cornerAccent =
    fighter.corner === "red" ? "bg-corner-red" : "bg-corner-blue";

  return (
    <div
      className={
        alignEnd
          ? "relative flex flex-col items-center gap-3 bg-card p-6 sm:items-end sm:text-right"
          : "relative flex flex-col items-center gap-3 bg-card p-6 sm:items-start"
      }
    >
      <span
        aria-hidden="true"
        className={`absolute inset-x-0 top-0 h-1 ${cornerAccent}`}
      />
      <Avatar
        name={fighter.name}
        src={fighter.headshot_url}
        className="size-20 text-xl"
      />
      <div className="flex flex-col gap-1">
        <span className="text-[0.6rem] font-medium uppercase tracking-[0.2em] text-muted-foreground">
          {fighter.corner === "red" ? "Canto vermelho" : "Canto azul"}
        </span>
        <Link
          to={`/fighters/${String(fighter.fighter_id)}`}
          aria-label={isWinner ? `${fighter.name}, vencedor` : undefined}
          className="font-display text-xl font-bold uppercase tracking-wide hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
    </div>
  );
}

/**
 * Os próximos eventos. A `EventList` na variante `upcoming` cuida do estado
 * vazio (catálogo dessincronizado) e do marcador "aguardando sincronização" nas
 * linhas de data já passada — o `gap_sync` é manual, então isso acontece.
 */
function UpcomingEventsSection() {
  const query = useEvents({
    when: "upcoming",
    limit: HOME_UPCOMING_LIMIT,
    offset: 0,
  });

  return (
    <section aria-labelledby="proximos-eventos" className="mt-14">
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-border pb-2">
        <h2
          id="proximos-eventos"
          className="font-display text-xl font-bold uppercase tracking-wide"
        >
          Próximos eventos
        </h2>
        <Link
          to="/events"
          className="font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground underline-offset-4 hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Ver todos os eventos
        </Link>
      </div>

      <EventList
        events={query.data?.items ?? []}
        variant="upcoming"
        isPending={query.isPending}
        isError={query.isError}
      />
    </section>
  );
}
