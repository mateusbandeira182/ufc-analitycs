import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { HeadToHeadOut } from "@/api/schema";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state do confronto direto entre dois lutadores. Só dispara quando os
 * dois ids estão definidos e são distintos — coerente com o 422 do backend para
 * `a == b`, mas sem ir à rede: a guarda client-side evita o request inútil.
 */
export function useHeadToHead(
  a: number | null,
  b: number | null,
): UseQueryResult<HeadToHeadOut> {
  return useQuery({
    queryKey: queryKeys.headToHead(a, b),
    queryFn: () => {
      // Nunca alcançável com a query desabilitada; a checagem satisfaz o tipo.
      if (a === null || b === null) {
        throw new Error("Confronto exige dois lutadores definidos.");
      }
      return apiGet<HeadToHeadOut>("/head-to-head", { a, b });
    },
    enabled: a !== null && b !== null && a !== b,
  });
}
