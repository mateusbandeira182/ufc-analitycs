import { apiGet } from "@/api/client";
import type { EventOut, EventWhen, PageEventOut } from "@/api/schema";

export interface EventsResult {
  items: EventOut[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * Busca eventos na API (paginação por `limit`/`offset`) e desembrulha o envelope
 * Page[EventOut] para a forma que a UI consome — preservando `total`/`limit`/
 * `offset` para a aritmética de páginas. A ordem é a que o backend entrega
 * (crescente em `upcoming`, decrescente nos demais); o cliente não reordena.
 *
 * `when` particiona o acervo server-side e o `total` da resposta passa a
 * refletir o conjunto filtrado — é dele que sai a contagem de páginas da seção.
 */
export async function getEvents(query?: {
  when?: EventWhen;
  limit?: number;
  offset?: number;
}): Promise<EventsResult> {
  const page = await apiGet<PageEventOut>("/events", {
    when: query?.when,
    limit: query?.limit,
    offset: query?.offset,
  });
  return {
    items: page.items,
    total: page.total,
    limit: page.limit,
    offset: page.offset,
  };
}
