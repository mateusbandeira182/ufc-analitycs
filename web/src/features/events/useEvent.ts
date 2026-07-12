import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { EventDetailOut } from "@/api/schema";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state do detalhe de um evento (cabeçalho + card de lutas). A resposta
 * já é o próprio EventDetailOut (sem envelope). Desabilitado quando o id é
 * inválido (`NaN`) para não disparar request — a página trata o id inválido como
 * não encontrado. O erro 404 chega como `ApiError` com `status`, distinguido na UI.
 */
export function useEvent(id: number): UseQueryResult<EventDetailOut> {
  return useQuery({
    queryKey: queryKeys.event(id),
    queryFn: () => apiGet<EventDetailOut>(`/events/${String(id)}`),
    enabled: !Number.isNaN(id),
  });
}
