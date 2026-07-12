import { apiGet } from "@/api/client";
import type { FighterOut, PageFighterOut } from "@/api/schema";

export interface FightersResult {
  items: FighterOut[];
  total: number;
}

/**
 * Busca lutadores na API (busca server-side por `name`) e desembrulha o envelope
 * Page[FighterOut] para a forma que a UI consome. O desembrulho é local a esta
 * slice de propósito (YAGNI — generalizar quando eventos entrarem).
 */
export async function getFighters(query?: {
  name?: string;
}): Promise<FightersResult> {
  const page = await apiGet<PageFighterOut>("/fighters", {
    name: query?.name,
  });
  return { items: page.items, total: page.total };
}
