import {
  keepPreviousData,
  useQuery,
  type UseQueryResult,
} from "@tanstack/react-query";

import { getEvents, type EventsResult } from "@/features/events/getEvents";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state da lista de eventos. A janela `limit`/`offset` (fonte da verdade:
 * a URL) entra na query key para cache previsível e vai como paginação
 * server-side. `keepPreviousData` mantém a página anterior visível durante a
 * troca — sem flicker ao paginar.
 */
export function useEvents(params: {
  limit: number;
  offset: number;
}): UseQueryResult<EventsResult> {
  const { limit, offset } = params;
  return useQuery({
    queryKey: queryKeys.events({ limit, offset }),
    queryFn: () => getEvents({ limit, offset }),
    placeholderData: keepPreviousData,
  });
}
