import { History } from "lucide-react";

import type { FeaturesFreshnessOut } from "@/api/schema";
import { formatIsoDate } from "@/lib/format";

/**
 * Comando que rematerializa o cache as-of (grão LUTADOR). Espelha o estágio da
 * pipeline porque o payload não o carrega: `FeaturesFreshnessOut` tem quatro
 * campos, por contrato da SPEC.
 *
 * `"--stage materialize"` é PREFIXO de `"--stage materialize-asof"`. Apontar o
 * estágio errado manda o usuário reconstruir o cache por LUTA, e o defasado
 * continua defasado — falha que só uma asserção negativa acusa.
 */
export const MATERIALIZE_ASOF_COMMAND =
  "python -m ingestion.features build --stage materialize-asof";

interface StaleFeaturesBadgeProps {
  freshness: FeaturesFreshnessOut;
}

/**
 * Aviso de que o cache as-of ficou atrás do granular, renderizado **na seção que
 * lê o dado defasado** — nunca num banner global, que o olho aprende a ignorar.
 * Foi exatamente assim que o cache por luta escondeu 21 features por meses: o
 * log avisava e ninguém lia.
 *
 * Componente único e compartilhado (D-10): as Sprints 08 e 09 reusam, nunca
 * criam um segundo. Presentacional puro — recebe o diagnóstico já carregado pela
 * seção, sem server-state próprio.
 *
 * Em dia (`stale: false`) não renderiza nada.
 */
export function StaleFeaturesBadge({ freshness }: StaleFeaturesBadgeProps) {
  if (!freshness.stale) {
    return null;
  }

  const materializedAt = formatMaterializedAt(freshness.materialized_at);

  return (
    <div
      role="status"
      className="rounded-md border border-belt-gold/40 bg-belt-gold/5 px-4 py-3 text-sm"
    >
      <p className="flex items-center gap-2 font-display text-xs font-semibold uppercase tracking-[0.2em] text-belt-gold">
        <History aria-hidden="true" className="size-3.5" />
        Dado defasado
      </p>
      <p className="mt-2 text-muted-foreground">
        {materializedAt === null
          ? "O cache de forma atual nunca foi materializado, então os números abaixo ainda não existem."
          : `O cache de forma atual foi materializado em ${materializedAt} e o acervo mudou desde então: cobre ${String(freshness.n_bouts_source)} lutas, contra ${String(freshness.n_bouts_current)} no granular.`}
      </p>
      <p className="mt-2 text-muted-foreground">
        Para atualizar, rode{" "}
        <code className="rounded bg-secondary px-1.5 py-0.5 font-mono text-xs text-foreground">
          {MATERIALIZE_ASOF_COMMAND}
        </code>
      </p>
    </div>
  );
}

/**
 * Data da última materialização, só até o dia. `formatIsoDate` de `@/lib/format`
 * faz `split("-")` e com um datetime ISO produziria `"01T00:00:00Z/09/2026"` —
 * lixo renderizado sem erro nenhum. `null` é um estado próprio, não uma data
 * faltando: o cache nunca foi materializado, e isso se diz com uma frase, não
 * com um traço mudo.
 */
function formatMaterializedAt(iso: string | null): string | null {
  return iso === null ? null : formatIsoDate(iso.slice(0, 10));
}
