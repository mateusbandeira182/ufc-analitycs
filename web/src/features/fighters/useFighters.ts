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
 * Server-state da lista de lutadores. O termo `name` e a janela `limit`/`offset`
 * (fonte da verdade: a URL) entram na query key para cache previsível e vão como
 * busca e paginação server-side. `keepPreviousData` mantém a página anterior
 * visível durante a troca — sem flicker ao paginar.
 */
export function useFighters(params: {
  name: string;
  limit: number;
  offset: number;
}): UseQueryResult<FightersResult> {
  const { name, limit, offset } = params;
  return useQuery({
    queryKey: queryKeys.fighters({ name, limit, offset }),
    queryFn: () => getFighters({ name: name || undefined, limit, offset }),
    placeholderData: keepPreviousData,
  });
}
