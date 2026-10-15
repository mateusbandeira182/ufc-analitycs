import type { BoutCardOut } from "@/api/schema";

export interface CardSectionGroup {
  /** Rótulo cru da Cito (`"Main Card"`, `"Prelims"`); `null` em card sem contexto. */
  section: string | null;
  bouts: BoutCardOut[];
}

/**
 * Agrupa o card por `card_section` **vindo do payload** — nunca derivado de
 * `bout_order` (RF-27): a fórmula é da Cito (`1000 × seção + posição`) e pode
 * mudar. A ordem dentro de cada grupo e a ordem entre grupos (primeira
 * aparição) saem da ordem de entrada, que o backend já entregou como
 * `bout_order asc nulls_last, id asc`. `bout_order` não é sequencial nem denso
 * — o payload real traz 1001 na luta principal, que é o **menor** valor — então
 * só a ordem relativa é utilizável e esta função não a recalcula.
 *
 * `card_section` nulo forma um grupo com `section: null`, pela mesma regra de
 * primeira aparição: o enriquecimento de contexto da Cito só cobre evento
 * recente e de forma irregular, logo card sem seção é caso comum, não exceção.
 *
 * Nenhuma luta é perdida nem duplicada: achatar a saída reproduz a entrada
 * (exatamente, na ordem, quando as seções chegam contíguas, como o servidor as
 * entrega). Consumida pela página do evento realizado e pela do card agendado.
 */
export function groupByCardSection(bouts: BoutCardOut[]): CardSectionGroup[] {
  const groups: CardSectionGroup[] = [];
  // Mapeia a seção ao grupo já aberto. `Map` aceita `null` como chave, então a
  // seção ausente não precisa de ramo próprio.
  const bySection = new Map<string | null, CardSectionGroup>();

  for (const bout of bouts) {
    const existing = bySection.get(bout.card_section);
    if (existing) {
      existing.bouts.push(bout);
      continue;
    }
    const group: CardSectionGroup = {
      section: bout.card_section,
      bouts: [bout],
    };
    bySection.set(bout.card_section, group);
    groups.push(group);
  }

  return groups;
}
