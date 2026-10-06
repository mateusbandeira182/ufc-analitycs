import {
  keepPreviousData,
  useQuery,
  type UseQueryResult,
} from "@tanstack/react-query";

import {
  getFighters,
  type FightersResult,
} from "@/features/fighters/getFighters";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state da lista de lutadores. O termo `q` (nome ou apelido) e a janela
 * `limit`/`offset` (fonte da verdade: a URL) entram na query key para cache
 * previsível e vão como busca e paginação server-side. `keepPreviousData`
 * mantém a página anterior visível durante a troca — sem flicker ao paginar.
 */
export function useFighters(params: {
  q: string;
  limit: number;
  offset: number;
}): UseQueryResult<FightersResult> {
  const { q, limit, offset } = params;
  return useQuery({
    queryKey: queryKeys.fighters({ q, limit, offset }),
    queryFn: () => getFighters({ q: q || undefined, limit, offset }),
    placeholderData: keepPreviousData,
  });
}
