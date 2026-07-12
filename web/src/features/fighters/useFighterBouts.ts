import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { FighterBoutOut } from "@/api/schema";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state do histórico de lutas de um lutador. O backend já entrega a lista
 * em ordem cronológica — o cliente apenas renderiza na ordem recebida.
 */
export function useFighterBouts(id: number): UseQueryResult<FighterBoutOut[]> {
  return useQuery({
    queryKey: queryKeys.fighterBouts(id),
    queryFn: () => apiGet<FighterBoutOut[]>(`/fighters/${String(id)}/bouts`),
  });
}
