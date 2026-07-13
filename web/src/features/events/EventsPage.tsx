import { Link, useSearchParams } from "react-router";

import { EventList } from "@/features/events/EventList";
import { useEvents } from "@/features/events/useEvents";
import { Pagination } from "@/components/ui/pagination";
import { getPageInfo, parsePaginationParams } from "@/lib/pagination";

/**
 * Rota `/events`: a URL é a fonte da verdade da paginação (`?limit=`/`?offset=`),
 * tornando a listagem compartilhável. Orquestra o server-state e delega os
 * estados (carregamento/erro/vazio/sucesso) à lista.
 */
export function EventsPage() {
  const [searchParams] = useSearchParams();
  const { limit, offset } = parsePaginationParams(searchParams);
  const query = useEvents({ limit, offset });

  const data = query.data;
  const pageInfo = data ? getPageInfo(data) : null;

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

      {pageInfo?.isOutOfRange ? (
        <OutOfRangeNotice />
      ) : (
        <>
          <EventList
            events={data?.items ?? []}
            isPending={query.isPending}
            isError={query.isError}
          />

          {data && pageInfo && pageInfo.totalPages > 1 ? (
            <Pagination
              total={data.total}
              limit={data.limit}
              offset={data.offset}
              label="eventos"
            />
          ) : null}
        </>
      )}
    </section>
  );
}

/**
 * Página fora do intervalo: o `offset` da URL aponta além do acervo (link
 * compartilhado desatualizado, por exemplo). Orienta o usuário de volta à
 * primeira página.
 */
function OutOfRangeNotice() {
  const [searchParams] = useSearchParams();
  const params = new URLSearchParams(searchParams);
  params.delete("offset");
  const query = params.toString();
  const firstPageHref = query ? `/events?${query}` : "/events";

  return (
    <div
      role="status"
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/50 px-6 py-16 text-center"
    >
      <p className="font-display text-lg font-semibold uppercase tracking-wide">
        Página fora do intervalo
      </p>
      <p className="max-w-sm text-sm text-muted-foreground">
        Não há eventos nesta página do acervo.
      </p>
      <Link
        to={firstPageHref}
        className="mt-2 font-display text-xs font-semibold uppercase tracking-widest text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        Voltar à primeira página
      </Link>
    </div>
  );
}
