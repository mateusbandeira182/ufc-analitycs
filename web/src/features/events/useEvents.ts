import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { getEvents, type EventsResult } from "@/features/events/getEvents";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state da lista de eventos. Chave estável `['events']`; a lista não é
 * parametrizada nesta slice (sem busca nem paginação na UI).
 */
export function useEvents(): UseQueryResult<EventsResult> {
  return useQuery({
    queryKey: queryKeys.events(),
    queryFn: getEvents,
  });
}
