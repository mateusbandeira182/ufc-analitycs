import { EventList } from "@/features/events/EventList";
import { useEvents } from "@/features/events/useEvents";

/**
 * Rota `/events`: orquestra o server-state da lista de eventos e delega os
 * estados (carregamento/erro/vazio/sucesso) à lista. Sem lógica de servidor na
 * apresentação.
 */
export function EventsPage() {
  const query = useEvents();

  return (
    <section className="mx-auto w-full max-w-5xl px-4 py-10">
      <header className="mb-6">
        <p className="font-display text-xs font-medium uppercase tracking-[0.3em] text-primary">
          Arquivo do octógono
        </p>
        <h1 className="font-display text-4xl font-bold uppercase tracking-tight">
          Eventos
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Percorra os cards do UFC e abra um evento para ver suas lutas.
        </p>
      </header>

      {query.isSuccess ? (
        <p className="mb-4 font-mono text-xs uppercase tracking-widest text-muted-foreground">
          {query.data.total} {query.data.total === 1 ? "evento" : "eventos"}
        </p>
      ) : null}

      <EventList
        events={query.data?.items ?? []}
        isPending={query.isPending}
        isError={query.isError}
      />
    </section>
  );
}
