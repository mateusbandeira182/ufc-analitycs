import { ChevronLeft, ChevronRight } from "lucide-react";
import { Link, useLocation, useSearchParams } from "react-router";

import type { EventWhen } from "@/api/schema";
import { EventList } from "@/features/events/EventList";
import {
  EVENTS_SECTION_PAGE_SIZE,
  hrefForSectionPage,
  offsetForSectionPage,
  parseSectionPage,
} from "@/features/events/sectionPages";
import { useEvents } from "@/features/events/useEvents";
import { cn } from "@/lib/utils";

/*
  As duas seções da rota `/events`. O nome do parâmetro de página é pt-BR e
  fixado pela CA-20: mexer numa seção reescreve só o parâmetro dela.
*/
const SECTIONS = [
  {
    when: "past",
    param: "realizados",
    id: "eventos-realizados",
    title: "Realizados",
    description: "Cards que já aconteceram, do mais recente ao mais antigo.",
  },
  {
    when: "upcoming",
    param: "proximos",
    id: "eventos-proximos",
    title: "Próximos",
    description: "Cards por vir, do mais próximo ao mais distante.",
  },
] as const satisfies readonly {
  when: EventWhen;
  param: string;
  id: string;
  title: string;
  description: string;
}[];

/**
 * Rota `/events`: duas seções com paginação **independente de verdade**. A URL é
 * a fonte da verdade de cada uma (`?realizados=3&proximos=1`), e cada seção tem
 * seu próprio server-state — a partição entra na query key, senão as duas leriam
 * a mesma entrada de cache e mostrariam a mesma lista.
 */
export function EventsPage() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-10">
      <header className="mb-10">
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

      <div className="grid gap-12">
        {SECTIONS.map((section) => (
          <EventsSection key={section.param} {...section} />
        ))}
      </div>
    </div>
  );
}

/**
 * Uma seção da lista: cabeçalho, total declarado, lista e sua própria
 * paginação. O `offset` sai do número de página desta seção; a outra não é lida
 * nem escrita aqui.
 */
function EventsSection({
  when,
  param,
  id,
  title,
  description,
}: {
  when: EventWhen;
  param: string;
  id: string;
  title: string;
  description: string;
}) {
  const [searchParams] = useSearchParams();
  const page = parseSectionPage(searchParams, param);
  const query = useEvents({
    when,
    limit: EVENTS_SECTION_PAGE_SIZE,
    offset: offsetForSectionPage(page),
  });

  const data = query.data;
  const totalPages = data
    ? Math.max(1, Math.ceil(data.total / EVENTS_SECTION_PAGE_SIZE))
    : 1;
  const isOutOfRange = data ? data.total > 0 && page > totalPages : false;

  return (
    <section aria-labelledby={id}>
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-border pb-2">
        <h2
          id={id}
          className="font-display text-xl font-bold uppercase tracking-wide"
        >
          {title}
        </h2>
        {data ? (
          <p className="font-mono text-xs uppercase tracking-widest tabular-nums text-muted-foreground">
            {data.total} {data.total === 1 ? "evento" : "eventos"}
          </p>
        ) : null}
      </div>
      <p className="mb-4 text-sm text-muted-foreground">{description}</p>

      {isOutOfRange ? (
        <OutOfRangeNotice param={param} title={title} />
      ) : (
        <>
          <EventList
            events={data?.items ?? []}
            variant={when}
            isPending={query.isPending}
            isError={query.isError}
          />

          {data && totalPages > 1 ? (
            <SectionPagination
              param={param}
              label={`eventos ${title.toLowerCase()}`}
              page={page}
              totalPages={totalPages}
            />
          ) : null}
        </>
      )}
    </section>
  );
}

/**
 * Paginação de uma seção: cada passo é um link que reescreve **só** o parâmetro
 * desta seção, preservando o da outra. Não reusa o `Pagination` compartilhado
 * porque lá a janela é `limit`/`offset` únicos, que não endereçam duas listas na
 * mesma tela.
 */
function SectionPagination({
  param,
  label,
  page,
  totalPages,
}: {
  param: string;
  label: string;
  page: number;
  totalPages: number;
}) {
  const [searchParams] = useSearchParams();
  const { pathname } = useLocation();

  return (
    <nav
      aria-label={`Paginação de ${label}`}
      className="mt-6 flex items-center justify-between gap-4 border-t border-border pt-4"
    >
      <PageButton
        to={hrefForSectionPage(pathname, searchParams, param, page - 1)}
        disabled={page <= 1}
        iconAfter={false}
      >
        Anterior
      </PageButton>

      <p
        aria-live="polite"
        className="font-mono text-xs uppercase tracking-widest tabular-nums text-muted-foreground"
      >
        Página {page} de {totalPages}
      </p>

      <PageButton
        to={hrefForSectionPage(pathname, searchParams, param, page + 1)}
        disabled={page >= totalPages}
        iconAfter
      >
        Próxima
      </PageButton>
    </nav>
  );
}

function PageButton({
  to,
  disabled,
  iconAfter,
  children,
}: {
  to: string;
  disabled: boolean;
  iconAfter: boolean;
  children: string;
}) {
  const icon = iconAfter ? (
    <ChevronRight aria-hidden="true" className="size-4" />
  ) : (
    <ChevronLeft aria-hidden="true" className="size-4" />
  );
  const layout = cn(
    "inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-2 font-display text-xs font-semibold uppercase tracking-widest",
    iconAfter && "flex-row-reverse",
  );

  if (disabled) {
    return (
      <span
        aria-disabled="true"
        className={cn(layout, "cursor-not-allowed text-muted-foreground/40")}
      >
        {icon}
        {children}
      </span>
    );
  }

  return (
    <Link
      to={to}
      className={cn(
        layout,
        "text-foreground transition-colors motion-reduce:transition-none hover:border-primary/60 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
      )}
    >
      {icon}
      {children}
    </Link>
  );
}

/**
 * Página fora do intervalo **desta** seção: o parâmetro da URL aponta além do
 * conjunto (link compartilhado desatualizado, por exemplo). O aviso é da seção
 * afetada e o link de volta zera só o parâmetro dela — a outra seção segue
 * mostrando sua lista, porque o problema não é dela.
 */
function OutOfRangeNotice({ param, title }: { param: string; title: string }) {
  const [searchParams] = useSearchParams();
  const { pathname } = useLocation();

  return (
    <div
      role="status"
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center"
    >
      <p className="font-display text-lg font-semibold uppercase tracking-wide">
        Página fora do intervalo
      </p>
      <p className="max-w-sm text-sm text-muted-foreground">
        Não há eventos nesta página de {title.toLowerCase()}.
      </p>
      <Link
        to={hrefForSectionPage(pathname, searchParams, param, 1)}
        className="mt-2 font-display text-xs font-semibold uppercase tracking-widest text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        Voltar à primeira página
      </Link>
    </div>
  );
}
