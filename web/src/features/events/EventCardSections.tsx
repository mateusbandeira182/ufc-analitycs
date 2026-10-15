import type { BoutCardOut, CardBoutPredictionOut } from "@/api/schema";
import { EventBoutCard } from "@/features/events/EventBoutCard";
import { groupByCardSection } from "@/features/events/cardOrder";

interface EventCardSectionsProps {
  bouts: BoutCardOut[];
  /**
   * Previsões do card indexadas por **`bout_id`**. Vazio quando o card não tem
   * luta agendada (a requisição nem sai) e enquanto ela carrega ou falha.
   */
  predictions: Map<number, CardBoutPredictionOut>;
}

/**
 * O card do evento agrupado por `card_section`. A divisão entre card principal
 * e preliminares é informação que vem do payload — por isso ela é estrutura na
 * tela (cada grupo é uma `section` com cabeçalho próprio) e não enfeite.
 *
 * A ordem, dentro e entre os grupos, é a do servidor. `bout_order` traz 1001 na
 * luta principal e não é sequencial nem denso, então nada aqui o lê.
 *
 * Card **misto** não é particionado nem reordenado por `status`: separar as
 * agendadas num grupo próprio pareceria organização e quebraria a ordem real do
 * card. Cada luta decide o próprio modo, dentro da seção em que o payload a pôs.
 */
export function EventCardSections({
  bouts,
  predictions,
}: EventCardSectionsProps) {
  const groups = groupByCardSection(bouts);

  return (
    <div className="grid gap-8">
      {groups.map((group) => {
        const label = group.section ?? "Lutas sem seção";
        const id = `secao-${slugify(label)}`;
        return (
          <section key={label} aria-labelledby={id}>
            <h3
              id={id}
              className="mb-3 flex items-center gap-3 font-display text-xs font-semibold uppercase tracking-[0.25em] text-muted-foreground"
            >
              {label}
              {/* Hairline: a divisão do card é real, então ela é desenhada. */}
              <span aria-hidden="true" className="h-px flex-1 bg-border" />
            </h3>
            <ul aria-label={`Lutas — ${label}`} className="grid gap-3">
              {group.bouts.map((bout) => (
                <EventBoutCard
                  key={bout.id}
                  bout={bout}
                  prediction={predictions.get(bout.id)}
                />
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

/** Rótulo cru da Cito como fragmento de id estável (`"Main Card"` -> `main-card`). */
function slugify(label: string): string {
  return label
    .toLocaleLowerCase("pt-BR")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}
