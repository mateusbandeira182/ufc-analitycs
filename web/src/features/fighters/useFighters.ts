import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import {
  getFighters,
  type FightersResult,
} from "@/features/fighters/getFighters";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state da lista de lutadores. O termo `name` (fonte da verdade: a URL)
 * entra na query key para cache previsível e é enviado como busca server-side.
 */
export function useFighters(name: string): UseQueryResult<FightersResult> {
  return useQuery({
    queryKey: queryKeys.fighters({ name }),
    queryFn: () => getFighters(name ? { name } : undefined),
  });
}
