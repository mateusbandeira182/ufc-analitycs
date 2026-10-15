import type { MatchupAttributeOut } from "@/api/schema";
import { formatAttributeValue } from "@/features/confronto/format";

interface AttributeComparisonProps {
  /** Atributos como a resposta os trouxe; o `label` é exibido, nunca redigitado. */
  attributes: MatchupAttributeOut[];
  nameA: string;
  nameB: string;
}

/**
 * Os atributos as-of **crus** dos dois lados, lado a lado: os números que formam
 * a probabilidade, no estado em que estavam antes desta luta.
 *
 * O `label` vem do payload (`MATCHUP_ATTRIBUTES` no backend) e carrega a própria
 * unidade — um mapa chave -> rótulo aqui apodreceria no dia em que o backend
 * acrescentasse o nono atributo.
 *
 * Presentacional puro. Lista vazia não renderiza a seção: é o que o contrato
 * chama de "não foi produzido" (DP-05e), e uma casca vazia diria menos que nada.
 */
export function AttributeComparison({
  attributes,
  nameA,
  nameB,
}: AttributeComparisonProps) {
  if (attributes.length === 0) {
    return null;
  }

  return (
    <section aria-labelledby="atributos-heading">
      <h2
        id="atributos-heading"
        className="font-display text-xl font-bold uppercase tracking-wide"
      >
        Atributos antes da luta
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">
        O estado de cada atleta na véspera deste confronto, sem contar o que
        veio depois.
      </p>

      <div className="mt-5 grid grid-cols-[1fr_4.5rem_4.5rem] items-end gap-x-3 border-b border-border pb-2 sm:grid-cols-[1fr_6rem_6rem]">
        <span className="font-display text-[0.65rem] font-semibold uppercase tracking-[0.2em] text-muted-foreground">
          Atributo
        </span>
        <span className="truncate text-right font-display text-xs font-semibold uppercase tracking-wide text-corner-red">
          {nameA}
        </span>
        <span className="truncate text-right font-display text-xs font-semibold uppercase tracking-wide text-corner-blue">
          {nameB}
        </span>
      </div>

      <ul className="divide-y divide-border">
        {attributes.map((attribute) => (
          <li
            key={attribute.key}
            aria-label={attribute.label}
            className="grid grid-cols-[1fr_4.5rem_4.5rem] items-baseline gap-x-3 py-2.5 sm:grid-cols-[1fr_6rem_6rem]"
          >
            <span className="text-sm">{attribute.label}</span>
            <span className="text-right font-mono text-sm tabular-nums">
              {formatAttributeValue(attribute.value_a)}
            </span>
            <span className="text-right font-mono text-sm tabular-nums">
              {formatAttributeValue(attribute.value_b)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
