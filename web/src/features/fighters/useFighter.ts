import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { FighterOut } from "@/api/schema";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state do detalhe de um lutador. A resposta já é o próprio FighterOut
 * (sem envelope), então o request central `apiGet` basta — sem desembrulho.
 */
export function useFighter(id: number): UseQueryResult<FighterOut> {
  return useQuery({
    queryKey: queryKeys.fighter(id),
    queryFn: () => apiGet<FighterOut>(`/fighters/${String(id)}`),
  });
}
