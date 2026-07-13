import type { ReactNode } from "react";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { Link, useLocation, useSearchParams } from "react-router";

import { getPageInfo } from "@/lib/pagination";
import { cn } from "@/lib/utils";

interface PaginationProps {
  /** Total de itens (do envelope Page[T]). */
  total: number;
  /** Itens por página em vigor (do envelope Page[T]). */
  limit: number;
  /** Deslocamento atual (do envelope Page[T]). */
  offset: number;
  /**
   * Nome plural da coleção paginada em pt-BR (ex.: "lutadores"), usado para
   * rotular a navegação de forma acessível.
   */
  label: string;
}

/**
 * Controle de paginação orientado por URL: cada botão é um link que preserva os
 * demais parâmetros (busca, por exemplo) e apenas ajusta o `offset` — a página
 * fica compartilhável e o histórico do navegador funciona. "Anterior" some na
 * primeira página e "Próxima" na última (viram texto desabilitado, não links).
 */
export function Pagination({ total, limit, offset, label }: PaginationProps) {
  const [searchParams] = useSearchParams();
  const { pathname } = useLocation();
  const { currentPage, totalPages, isFirst, isLast } = getPageInfo({
    total,
    limit,
    offset,
  });

  function hrefForOffset(nextOffset: number): string {
    const next = new URLSearchParams(searchParams);
    if (nextOffset <= 0) {
      next.delete("offset");
    } else {
      next.set("offset", String(nextOffset));
    }
    const query = next.toString();
    return query ? `${pathname}?${query}` : pathname;
  }

  return (
    <nav
      aria-label={`Paginação de ${label}`}
      className="mt-8 flex items-center justify-between gap-4 border-t border-border pt-6"
    >
      <PageButton
        to={hrefForOffset(offset - limit)}
        disabled={isFirst}
        icon={<ChevronLeft aria-hidden="true" className="size-4" />}
      >
        Anterior
      </PageButton>

      <p
        aria-live="polite"
        className="font-mono text-xs uppercase tracking-widest tabular-nums text-muted-foreground"
      >
        Página {currentPage} de {totalPages}
      </p>

      <PageButton
        to={hrefForOffset(offset + limit)}
        disabled={isLast}
        icon={<ChevronRight aria-hidden="true" className="size-4" />}
        iconAfter
      >
        Próxima
      </PageButton>
    </nav>
  );
}

/**
 * Um passo da paginação: link quando habilitado, texto inerte (`aria-disabled`)
 * na borda. A cor do canto marca o foco; a transição respeita `prefers-reduced-motion`.
 */
function PageButton({
  to,
  disabled,
  icon,
  iconAfter = false,
  children,
}: {
  to: string;
  disabled: boolean;
  icon: ReactNode;
  iconAfter?: boolean;
  children: ReactNode;
}) {
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
