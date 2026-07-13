/*
  Utilitários de paginação compartilhados pelas listas (lutadores, eventos).
  A URL é a fonte da verdade de `limit`/`offset`; o envelope Page[T] do backend
  (total/limit/offset) é a fonte da verdade da aritmética de páginas. Concentrar
  a lógica aqui evita divergência entre a página e o controle de paginação.
*/

/** Tamanho de página padrão quando a URL não especifica `limit`. */
export const DEFAULT_PAGE_SIZE = 24;

export interface PaginationParams {
  limit: number;
  offset: number;
}

/**
 * Lê `limit` e `offset` da URL com defaults seguros. Valores não numéricos, zero
 * ou negativos caem no padrão (limit) ou em zero (offset) — a lista nunca fica
 * sem página válida por causa de uma query string manipulada.
 */
export function parsePaginationParams(
  params: URLSearchParams,
  defaultLimit: number = DEFAULT_PAGE_SIZE,
): PaginationParams {
  const limit = toPositiveInt(params.get("limit"), defaultLimit);
  const offset = toNonNegativeInt(params.get("offset"), 0);
  return { limit, offset };
}

export interface PageInfo {
  currentPage: number;
  totalPages: number;
  isFirst: boolean;
  isLast: boolean;
  /** Offset aponta para além do último item existente (com total > 0). */
  isOutOfRange: boolean;
}

/**
 * Deriva a informação de navegação a partir do envelope paginado. Total zero é
 * tratado como uma única página vazia (nem primeira nem fora do intervalo — só
 * não há o que paginar).
 */
export function getPageInfo({
  total,
  limit,
  offset,
}: {
  total: number;
  limit: number;
  offset: number;
}): PageInfo {
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const currentPage = Math.floor(offset / limit) + 1;
  return {
    currentPage,
    totalPages,
    isFirst: offset <= 0,
    isLast: offset + limit >= total,
    isOutOfRange: total > 0 && offset >= total,
  };
}

function toPositiveInt(raw: string | null, fallback: number): number {
  const value = Number(raw);
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

function toNonNegativeInt(raw: string | null, fallback: number): number {
  const value = Number(raw);
  return Number.isInteger(value) && value >= 0 ? value : fallback;
}
