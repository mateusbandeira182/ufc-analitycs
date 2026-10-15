/*
  Aritmética da paginação independente das duas seções da lista de Eventos
  (`?realizados=N&proximos=N`, CA-20). Pura e testável: a URL é a fonte da
  verdade de cada seção, e mexer numa **nunca** toca o parâmetro da outra.

  Por que não reusar `lib/pagination.ts`: lá a janela é `limit`/`offset` únicos,
  que não endereçam duas listas na mesma tela. Aqui o parâmetro é o número de
  página de uma seção nomeada, e o `limit` é constante.
*/

/** Itens por página de cada seção da lista de Eventos. */
export const EVENTS_SECTION_PAGE_SIZE = 10;

/**
 * Página 1-based da seção lida da URL. Valor ausente, não numérico, não inteiro
 * ou `<= 0` cai em 1 — a seção nunca fica sem página válida por causa de uma
 * query string manipulada ou de um link antigo.
 */
export function parseSectionPage(
  params: URLSearchParams,
  name: string,
): number {
  const raw = params.get(name);
  if (raw === null || raw.trim() === "") {
    return 1;
  }
  const value = Number(raw);
  return Number.isInteger(value) && value > 0 ? value : 1;
}

/** Offset que o backend espera para a página 1-based desta seção. */
export function offsetForSectionPage(page: number): number {
  return (page - 1) * EVENTS_SECTION_PAGE_SIZE;
}

/**
 * Href que troca **só** o parâmetro desta seção, preservando o da outra
 * (CA-20): paginar "Realizados" não pode mover "Próximos". A página 1 remove o
 * parâmetro, mantendo a URL limpa — é o padrão, e `?realizados=1` diria o mesmo
 * com mais ruído.
 */
export function hrefForSectionPage(
  pathname: string,
  params: URLSearchParams,
  name: string,
  page: number,
): string {
  const next = new URLSearchParams(params);
  if (page <= 1) {
    next.delete(name);
  } else {
    next.set(name, String(page));
  }
  const query = next.toString();
  return query ? `${pathname}?${query}` : pathname;
}
