import type { ReactNode } from "react";

import {
  AlertTriangle,
  CalendarDays,
  ChevronRight,
  MapPin,
} from "lucide-react";
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

/**
 * Entrada do arquivo de eventos: faixa lateral no ouro do cinturão (identidade do
 * evento, distinta do vermelho do lutador), nome em destaque, a data em
 * monoespaçada dourada e o local com marcador. A seta convida a abrir o card e
 * respeita `prefers-reduced-motion`.
 */
function EventRow({ event }: { event: EventOut }) {
  return (
    <Link
      to={`/events/${String(event.id)}`}
      className="group block rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <Card className="relative flex items-center gap-4 overflow-hidden p-4 pl-5 transition-colors motion-reduce:transition-none group-hover:border-primary/60">
        {/* Faixa do cinturão — identidade do evento. */}
        <span
          aria-hidden="true"
          className="absolute inset-y-0 left-0 w-1 bg-belt-gold/70"
        />
        <div className="min-w-0 flex-1">
          <h2 className="truncate font-display text-lg font-semibold uppercase tracking-wide transition-colors motion-reduce:transition-none group-hover:text-primary">
            {event.name}
          </h2>
          <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs uppercase tracking-wide">
            <span className="inline-flex items-center gap-1.5 font-mono tabular-nums text-belt-gold">
              <CalendarDays aria-hidden="true" className="size-3.5" />
              {formatIsoDate(event.date)}
            </span>
            {event.location ? (
              <span className="inline-flex items-center gap-1 text-muted-foreground">
                <MapPin aria-hidden="true" className="size-3.5" />
                {event.location}
              </span>
            ) : null}
          </p>
        </div>
        <ChevronRight
          aria-hidden="true"
          className="size-5 shrink-0 text-muted-foreground/40 transition-transform motion-reduce:transition-none group-hover:translate-x-0.5 group-hover:text-primary"
        />
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
