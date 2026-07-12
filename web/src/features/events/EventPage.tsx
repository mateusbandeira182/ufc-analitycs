import type { ReactNode } from "react";

import { AlertTriangle, CalendarDays, CalendarX, MapPin } from "lucide-react";
import { useParams } from "react-router";

import { ApiError } from "@/api/client";
import type { EventDetailOut } from "@/api/schema";
import { Skeleton } from "@/components/ui/skeleton";
import { EventBoutCard } from "@/features/events/EventBoutCard";
import { useEvent } from "@/features/events/useEvent";
import { formatIsoDate } from "@/lib/format";

/**
 * Rota `/events/:id`: orquestra o detalhe do evento. Lê o id da URL, trata id
 * inválido e inexistente (404) como não encontrado, delega o server-state ao
 * `useEvent` e monta cabeçalho + card de lutas.
 */
export function EventPage() {
  const { id } = useParams();
  const eventId = Number(id);
  const { data, isPending, isError, error } = useEvent(eventId);

  // Id de rota inválido (ex.: `/events/abc`): tratado como não encontrado, sem
  // disparar request (o hook fica desabilitado).
  if (Number.isNaN(eventId)) {
    return <NotFoundScreen />;
  }

  if (isPending) {
    return <EventPageSkeleton />;
  }

  if (isError) {
    const notFound = error instanceof ApiError && error.status === 404;
    return notFound ? (
      <NotFoundScreen />
    ) : (
      <StatusScreen
        icon={<AlertTriangle className="size-6 text-primary" />}
        title="Não foi possível carregar o evento"
        description="A API não respondeu como esperado. Tente novamente em instantes."
      />
    );
  }

  return (
    <section className="mx-auto w-full max-w-5xl px-4 py-10">
      <EventHeader event={data} />

      <div className="mt-10">
        <h2 className="mb-4 font-display text-xl font-bold uppercase tracking-wide">
          Card de lutas
        </h2>
        {data.bouts.length === 0 ? (
          <p role="status" className="text-sm text-muted-foreground">
            Nenhuma luta registrada para este evento.
          </p>
        ) : (
          <ul aria-label="Lutas do evento" className="grid gap-3">
            {data.bouts.map((bout) => (
              <EventBoutCard key={bout.id} bout={bout} />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function EventHeader({ event }: { event: EventDetailOut }) {
  return (
    <header className="mb-6">
      <p className="font-display text-xs font-medium uppercase tracking-[0.3em] text-primary">
        Card do evento
      </p>
      <h1 className="font-display text-4xl font-bold uppercase tracking-tight">
        {event.name}
      </h1>
      <p className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <CalendarDays aria-hidden="true" className="size-4" />
          {formatIsoDate(event.date)}
        </span>
        {event.location ? (
          <span className="inline-flex items-center gap-1.5">
            <MapPin aria-hidden="true" className="size-4" />
            {event.location}
          </span>
        ) : null}
      </p>
    </header>
  );
}

function EventPageSkeleton() {
  return (
    <section
      className="mx-auto w-full max-w-5xl px-4 py-10"
      aria-busy="true"
      aria-label="Carregando evento"
    >
      <Skeleton className="h-10 w-64" />
      <Skeleton className="mt-6 h-24 w-full" />
      <Skeleton className="mt-3 h-24 w-full" />
    </section>
  );
}

function NotFoundScreen() {
  return (
    <StatusScreen
      icon={<CalendarX className="size-6 text-muted-foreground" />}
      title="Evento não encontrado"
      description="Não há nenhum evento com esse identificador no acervo."
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
