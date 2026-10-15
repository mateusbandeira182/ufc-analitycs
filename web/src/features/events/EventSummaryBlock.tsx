import type { EventSummaryOut } from "@/api/schema";

interface EventSummaryBlockProps {
  /** `null` quando o evento não tem luta realizada classificável. */
  summary: EventSummaryOut | null;
}

/*
  Os seis baldes, na ordem em que se leem num card: os desfechos frequentes
  primeiro, os raros depois. A soma é igual a `n_bouts` por construção no
  backend — a tela declara o denominador e não recalcula nada.
*/
const BUCKETS = [
  { key: "ko_tko", label: "KO/TKO" },
  { key: "submissions", label: "Finalizações" },
  { key: "decisions", label: "Decisões" },
  { key: "draws", label: "Empates" },
  { key: "no_contests", label: "Sem resultado" },
  { key: "dq", label: "Desqualificações" },
] as const satisfies readonly { key: keyof EventSummaryOut; label: string }[];

/**
 * Contagem por método das lutas **realizadas** do card, sempre acompanhada do
 * denominador (`n_bouts`): num card misto, "4 KO/TKO" sem o "de 13 lutas
 * realizadas" se leria como o card inteiro.
 *
 * `summary` nulo **não renderiza nada**. Zero finalização é uma afirmação sobre
 * o evento; ausência de resultado não é zero, e uma fileira de zeros num card
 * inteiro agendado se leria como medição. Já um zero que **chega** no payload é
 * medição e aparece como zero.
 */
export function EventSummaryBlock({ summary }: EventSummaryBlockProps) {
  if (summary === null) {
    return null;
  }

  return (
    <section aria-labelledby="evento-resumo">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2
          id="evento-resumo"
          className="font-display text-sm font-bold uppercase tracking-[0.2em]"
        >
          Como terminou
        </h2>
        <p className="font-mono text-xs uppercase tracking-widest tabular-nums text-muted-foreground">
          {summary.n_bouts}{" "}
          {summary.n_bouts === 1 ? "luta realizada" : "lutas realizadas"}
        </p>
      </div>

      <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border sm:grid-cols-3 lg:grid-cols-6">
        {BUCKETS.map(({ key, label }) => (
          <div key={key} className="flex flex-col gap-1 bg-card px-3 py-3">
            <dt className="font-display text-[0.65rem] font-medium uppercase tracking-widest text-muted-foreground">
              {label}
            </dt>
            <dd className="font-mono text-2xl font-bold leading-none tabular-nums">
              {summary[key]}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
