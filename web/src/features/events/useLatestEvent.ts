import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import type { EventDetailOut } from "@/api/schema";
import { getLatestEvent } from "@/features/events/getLatestEvent";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state do card do último evento realizado na Home. Sem parâmetro: o
 * recorte é do servidor (o evento mais recente com luta realizada), então a
 * chave é constante. O 404 de acervo sem evento realizado chega como `ApiError`
 * e a Home o distingue de uma falha de API.
 */
export function useLatestEvent(): UseQueryResult<EventDetailOut> {
  return useQuery({
    queryKey: queryKeys.latestEvent(),
    queryFn: getLatestEvent,
  });
}
