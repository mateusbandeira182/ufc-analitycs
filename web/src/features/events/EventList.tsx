import type { ReactNode } from "react";

import { AlertTriangle, CalendarDays, MapPin } from "lucide-react";
import { Link } from "react-router";

import type { EventOut } from "@/api/schema";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatIsoDate } from "@/lib/format";

interface EventListProps {
  events: EventOut[];
  isPending: boolean;
  isError: boolean;
}

/**
 * Renderiza os quatro estados observáveis da lista de eventos a partir de props:
 * carregamento (skeleton), erro (mensagem legível), vazio e sucesso. Sem lógica
 * de servidor aqui — os dados vêm do hook via a página.
 */
export function EventList({ events, isPending, isError }: EventListProps) {
  if (isPending) {
    return (
      <div
        data-testid="events-loading"
        aria-busy="true"
        aria-label="Carregando eventos"
        className="grid gap-3"
      >
        {Array.from({ length: 6 }).map((_, index) => (
          <Skeleton key={index} className="h-20 w-full" />
        ))}
      </div>
    );
  }

  if (isError) {
    return (
      <StatusMessage
        icon={<AlertTriangle className="size-6 text-primary" />}
        title="Não foi possível carregar os eventos"
        description="A API não respondeu como esperado. Tente novamente em instantes."
      />
    );
  }

  if (events.length === 0) {
    return (
      <StatusMessage
        icon={<CalendarDays className="size-6 text-muted-foreground" />}
        title="Nenhum evento encontrado"
        description="Ainda não há eventos no acervo para exibir."
      />
    );
  }

  return (
    <ul aria-label="Eventos" className="grid gap-3">
      {events.map((event) => (
        <li key={event.id}>
          <EventRow event={event} />
        </li>
      ))}
    </ul>
  );
}

function EventRow({ event }: { event: EventOut }) {
  return (
    <Link
      to={`/events/${String(event.id)}`}
      className="group block rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <Card className="relative flex items-center gap-4 overflow-hidden p-4 transition-colors group-hover:border-primary/60">
        {/* Faixa de acento — identidade visual do evento. */}
        <span
          aria-hidden="true"
          className="absolute inset-y-0 left-0 w-1 bg-primary"
        />
        <div className="min-w-0 flex-1 pl-2">
          <h2 className="truncate font-display text-lg font-semibold uppercase tracking-wide">
            {event.name}
          </h2>
          <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs uppercase tracking-wide text-muted-foreground">
            <span className="inline-flex items-center gap-1">
              <CalendarDays aria-hidden="true" className="size-3.5" />
              {formatIsoDate(event.date)}
            </span>
            {event.location ? (
              <span className="inline-flex items-center gap-1">
                <MapPin aria-hidden="true" className="size-3.5" />
                {event.location}
              </span>
            ) : null}
          </p>
        </div>
      </Card>
    </Link>
  );
}

function StatusMessage({
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
