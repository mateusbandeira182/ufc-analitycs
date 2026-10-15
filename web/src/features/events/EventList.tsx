import type { ReactNode } from "react";

import {
  AlertTriangle,
  CalendarDays,
  ChevronRight,
  MapPin,
  RefreshCcw,
} from "lucide-react";
import { Link } from "react-router";

import type { EventOut } from "@/api/schema";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { isAwaitingSync, todayIsoDate } from "@/features/events/syncStatus";
import { formatIsoDate } from "@/lib/format";

/**
 * Qual das duas listas de evento esta é. Controla **duas** coisas e nada mais:
 * a cópia do estado vazio e o marcador "aguardando sincronização" nas linhas de
 * `upcoming` cuja data já passou. Não decide modo de página nenhum.
 */
export type EventListVariant = "past" | "upcoming";

interface EventListProps {
  events: EventOut[];
  variant: EventListVariant;
  isPending: boolean;
  isError: boolean;
  /**
   * Data de hoje como `YYYY-MM-DD`, injetável para o teste não depender do
   * relógio. O padrão é o dia corrente, que é o que a tela quer.
   */
  today?: string;
}

/**
 * Renderiza os quatro estados observáveis de uma lista de eventos a partir de
 * props: carregamento (skeleton), erro (mensagem legível), vazio (cópia por
 * variante) e sucesso. Sem lógica de servidor aqui — os dados vêm do hook via a
 * página.
 */
export function EventList({
  events,
  variant,
  isPending,
  isError,
  today = todayIsoDate(),
}: EventListProps) {
  if (isPending) {
    return (
      <div
        data-testid="events-loading"
        aria-busy="true"
        aria-label="Carregando eventos"
        className="grid gap-3"
      >
        {Array.from({ length: 4 }).map((_, index) => (
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
    return variant === "upcoming" ? (
      // Nunca bloco vazio e mudo: ausência de evento por vir é um fato sobre a
      // sincronização do catálogo, não sobre o acervo — que tem 30 anos de UFC.
      <StatusMessage
        icon={<RefreshCcw className="size-6 text-belt-gold" />}
        title="Catálogo dessincronizado"
        description="Nenhum evento por vir no acervo. A ingestão do próximo card ainda não rodou."
      />
    ) : (
      <StatusMessage
        icon={<CalendarDays className="size-6 text-muted-foreground" />}
        title="Nenhum evento encontrado"
        description="Não há eventos realizados nesta parte do acervo."
      />
    );
  }

  return (
    <ul
      aria-label={
        variant === "past" ? "Eventos realizados" : "Próximos eventos"
      }
      className="grid gap-3"
    >
      {events.map((event) => (
        <EventRow
          key={event.id}
          event={event}
          awaitingSync={
            variant === "upcoming" && isAwaitingSync(event.date, today)
          }
        />
      ))}
    </ul>
  );
}

/**
 * Entrada do arquivo de eventos: faixa lateral no ouro do cinturão (identidade
 * do evento, distinta do vermelho do lutador), nome em destaque, a data em
 * monoespaçada dourada e o local com marcador. A linha inteira é clicável e a
 * seta convida a abrir o card, respeitando `prefers-reduced-motion`.
 */
function EventRow({
  event,
  awaitingSync,
}: {
  event: EventOut;
  awaitingSync: boolean;
}) {
  return (
    <li aria-label={event.name}>
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
            <h3 className="truncate font-display text-lg font-semibold uppercase tracking-wide transition-colors motion-reduce:transition-none group-hover:text-primary">
              {event.name}
            </h3>
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
              {awaitingSync ? <AwaitingSyncMarker /> : null}
            </p>
          </div>
          <ChevronRight
            aria-hidden="true"
            className="size-5 shrink-0 text-muted-foreground/40 transition-transform motion-reduce:transition-none group-hover:translate-x-0.5 group-hover:text-primary"
          />
        </Card>
      </Link>
    </li>
  );
}

/**
 * O evento está em "Próximos" com a data já passada. Isso não é erro de tela: o
 * backend particiona pelo `status` da luta e o `gap_sync` é manual, então a
 * linha está dizendo que o resultado ainda não foi ingerido. Sem o marcador, uma
 * tabela de "Próximos" com datas passadas seria uma contradição muda.
 */
function AwaitingSyncMarker() {
  return (
    <span className="inline-flex items-center gap-1 rounded-sm border border-belt-gold/40 px-1.5 py-0.5 font-display text-[0.6rem] font-semibold tracking-widest text-belt-gold">
      <RefreshCcw aria-hidden="true" className="size-3" />
      Aguardando sincronização
    </span>
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
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center"
    >
      {icon}
      <p className="font-display text-lg font-semibold uppercase tracking-wide">
        {title}
      </p>
      <p className="max-w-sm text-sm text-muted-foreground">{description}</p>
    </div>
  );
}
