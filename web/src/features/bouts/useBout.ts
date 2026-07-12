import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { BoutDetailOut } from "@/api/schema";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state do detalhe de uma luta (evento, resultado e os dois cantos com
 * stats granulares). A resposta já é o próprio BoutDetailOut (sem envelope) e cada
 * canto traz o nome do lutador — não é preciso buscar lutador à parte. Desabilitado
 * quando o id é inválido (`NaN`) para não disparar request; a página trata o id
 * inválido como não encontrado. O 404 chega como `ApiError` com `status`.
 */
export function useBout(id: number): UseQueryResult<BoutDetailOut> {
  return useQuery({
    queryKey: queryKeys.bout(id),
    queryFn: () => apiGet<BoutDetailOut>(`/bouts/${String(id)}`),
    enabled: !Number.isNaN(id),
  });
}
