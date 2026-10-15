import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { MatchupPredictionOut } from "@/api/schema";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state do palpite neutro de canto entre dois lutadores. Só dispara quando
 * os dois ids estão definidos — a página os fornece apenas ao clicar em "Prever".
 * A ordem A/B não altera o resultado (o backend neutraliza o canto), e o 422 de
 * lutadores iguais é tratado como erro na UI, não barrado aqui: o endpoint é a
 * fonte de verdade da validação.
 */
export function useMatchupPrediction(
  a: number | null,
  b: number | null,
): UseQueryResult<MatchupPredictionOut> {
  return useQuery({
    queryKey: queryKeys.matchup(a, b),
    queryFn: () => {
      // Nunca alcançável com a query desabilitada; a checagem satisfaz o tipo.
      if (a === null || b === null) {
        throw new Error("O palpite exige dois lutadores definidos.");
      }
      return apiGet<MatchupPredictionOut>("/predict/matchup", {
        fighter_a: a,
        fighter_b: b,
      });
    },
    enabled: a !== null && b !== null,
  });
}
