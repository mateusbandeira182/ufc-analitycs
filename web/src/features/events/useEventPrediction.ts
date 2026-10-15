import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { EventPredictionOut } from "@/api/schema";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state do card previsto de um evento, uma entrada por luta.
 *
 * `enabled` entra por **parâmetro** e não é derivado do id: `GET
 * /predict/event/{id}` **escreve** em `bout_predictions` (`source="api"`, com
 * commit no service), e o destaque da maior zebra lê essa tabela. Disparar a
 * requisição na página de um evento realizado gravaria palpites as-of-hoje sobre
 * lutas passadas e mudaria a "maior zebra" e o denominador de lutas previstas do
 * próprio evento e da Home — a cada render. Quem decide é a página, que sabe se
 * o card tem luta `scheduled`.
 *
 * Desabilitado também com id inválido (`NaN`), para não emitir
 * `/predict/event/NaN`; a página trata o id inválido como não encontrado.
 */
export function useEventPrediction(
  eventId: number,
  enabled: boolean,
): UseQueryResult<EventPredictionOut> {
  return useQuery({
    queryKey: queryKeys.eventPrediction(eventId),
    queryFn: () =>
      apiGet<EventPredictionOut>(`/predict/event/${String(eventId)}`),
    enabled: enabled && !Number.isNaN(eventId),
  });
}
