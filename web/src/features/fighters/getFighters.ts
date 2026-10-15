import { apiGet } from "@/api/client";
import type { FighterOut, PageFighterOut } from "@/api/schema";

export interface FightersResult {
  items: FighterOut[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * Busca lutadores na API (busca server-side por `q`, que casa **nome ou
 * apelido**, paginação por `limit`/`offset`) e desembrulha o envelope
 * Page[FighterOut] para a forma que a UI consome — preservando
 * `total`/`limit`/`offset` para a aritmética de páginas. O desembrulho é local a
 * esta slice de propósito (YAGNI).
 */
export async function getFighters(query?: {
  q?: string | undefined;
  limit?: number | undefined;
  offset?: number | undefined;
}): Promise<FightersResult> {
  const page = await apiGet<PageFighterOut>("/fighters", {
    q: query?.q,
    limit: query?.limit,
    offset: query?.offset,
  });
  return {
    items: page.items,
    total: page.total,
    limit: page.limit,
    offset: page.offset,
  };
}
