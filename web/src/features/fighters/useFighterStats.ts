import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { FighterStatsOut } from "@/api/schema";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state das estatísticas resumidas de um lutador (médias + perfil de
 * striking), computadas on demand pelo backend em `/fighters/:id/stats`. A página
 * de estatísticas consome daqui o `striking_profile` (career-level); as médias por
 * recorte seguem derivadas do histórico granular. Desabilitado quando o id é
 * inválido (`NaN`) para não disparar request, coerente com `useFighter`.
 */
export function useFighterStats(id: number): UseQueryResult<FighterStatsOut> {
  return useQuery({
    queryKey: queryKeys.fighterStats(id),
    queryFn: () => apiGet<FighterStatsOut>(`/fighters/${String(id)}/stats`),
    enabled: !Number.isNaN(id),
  });
}
