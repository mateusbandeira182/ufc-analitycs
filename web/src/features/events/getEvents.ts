import { apiGet } from "@/api/client";
import type { EventOut, PageEventOut } from "@/api/schema";

export interface EventsResult {
  items: EventOut[];
  total: number;
}

/**
 * Busca os eventos na API e desembrulha o envelope Page[EventOut] para a forma
 * que a UI consome. Consome a primeira página (limit/offset padrão do backend);
 * paginação não pertence a esta slice. A ordem (mais recentes primeiro) é a que
 * o backend entrega — o cliente não reordena.
 */
export async function getEvents(): Promise<EventsResult> {
  const page = await apiGet<PageEventOut>("/events");
  return { items: page.items, total: page.total };
}
