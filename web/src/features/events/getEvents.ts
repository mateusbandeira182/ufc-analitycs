import { apiGet } from "@/api/client";
import type { EventOut, PageEventOut } from "@/api/schema";

export interface EventsResult {
  items: EventOut[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * Busca eventos na API (paginação por `limit`/`offset`) e desembrulha o envelope
 * Page[EventOut] para a forma que a UI consome — preservando `total`/`limit`/
 * `offset` para a aritmética de páginas. A ordem (mais recentes primeiro) é a que
 * o backend entrega; o cliente não reordena.
 */
export async function getEvents(query?: {
  limit?: number;
  offset?: number;
}): Promise<EventsResult> {
  const page = await apiGet<PageEventOut>("/events", {
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
