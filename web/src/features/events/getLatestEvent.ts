import { apiGet } from "@/api/client";
import type { EventDetailOut } from "@/api/schema";

/**
 * Busca o último evento **realizado** (`GET /events/latest`) para o card da
 * Home. A resposta é o próprio `EventDetailOut` — a Home precisa exatamente da
 * mesma forma do detalhe (identidade + card + resumo + destaques), e um schema
 * próprio sem campo novo só duplicaria o contrato.
 *
 * O backend responde **404** quando o acervo não tem nenhum evento realizado,
 * em vez de 200 com corpo nulo: o corpo nulo tornaria o tipo anulável em todo
 * consumidor por causa de um estado de acervo vazio. O 404 chega como
 * `ApiError` e a Home o trata como estado vazio declarado.
 */
export function getLatestEvent(): Promise<EventDetailOut> {
  return apiGet<EventDetailOut>("/events/latest");
}
