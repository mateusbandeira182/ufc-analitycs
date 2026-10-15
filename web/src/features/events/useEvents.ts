import {
  keepPreviousData,
  useQuery,
  type UseQueryResult,
} from "@tanstack/react-query";

import type { EventWhen } from "@/api/schema";
import { getEvents, type EventsResult } from "@/features/events/getEvents";
import { queryKeys } from "@/queryKeys";

/**
 * Server-state de uma seção da lista de eventos. A partição (`when`) e a janela
 * `limit`/`offset` — fonte da verdade: a URL — entram na query key e vão como
 * filtro e paginação server-side. `keepPreviousData` mantém a página anterior
 * visível durante a troca, sem flicker ao paginar.
 *
 * `when` é **obrigatório**: as duas seções da página de Eventos convivem na
 * mesma tela, e uma chave que o omitisse faria as duas lerem a mesma entrada de
 * cache e exibirem a mesma lista.
 */
export function useEvents(params: {
  when: EventWhen;
  limit: number;
  offset: number;
}): UseQueryResult<EventsResult> {
  const { when, limit, offset } = params;
  return useQuery({
    queryKey: queryKeys.events({ when, limit, offset }),
    queryFn: () => getEvents({ when, limit, offset }),
    placeholderData: keepPreviousData,
  });
}
