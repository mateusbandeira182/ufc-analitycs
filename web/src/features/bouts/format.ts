import type { BoutDetailOut } from "@/api/schema";
import { DASH } from "@/lib/format";

/*
  Formatadores específicos da página da luta. Os genéricos (data, método, tempo de
  encerramento com round, duração m:ss) vivem em `@/lib/format`; aqui ficam os de
  apresentação do box-score granular — pares acertados/tentados e a resolução do
  vencedor por nome. `formatDuration` foi promovido para `@/lib/format` quando a
  página de estatísticas passou a precisar dele; reexportado aqui para preservar
  os call sites e testes desta feature.
*/
export { formatDuration } from "@/lib/format";

/** Número da stat como texto; preserva o zero e usa traço quando nulo. */
export function formatStat(value: number | null): string {
  return value === null ? DASH : String(value);
}

/**
 * Par acertados/tentados como `"x de y"` (ex.: golpes significativos, quedas).
 * Cada campo é anulável no contrato: usa traço no campo ausente; quando ambos são
 * nulos, colapsa num único traço (não há dado a comparar).
 */
export function formatAttempts(
  landed: number | null,
  attempted: number | null,
): string {
  if (landed === null && attempted === null) {
    return DASH;
  }
  return `${formatStat(landed)} de ${formatStat(attempted)}`;
}

/** Resultado da luta pela ótica de quem venceu (ou a ausência de vencedor). */
export type BoutResult =
  { kind: "winner"; name: string } | { kind: "draw" } | { kind: "no_contest" };

/**
 * Deriva o resultado a partir do detalhe da luta. No contest tem método próprio;
 * o empate mora na nulabilidade do vencedor (não há método `draw`). Havendo
 * vencedor, resolve o nome cruzando `winner_id` com o canto correspondente.
 */
export function resolveResult(bout: BoutDetailOut): BoutResult {
  if (bout.method === "no_contest") {
    return { kind: "no_contest" };
  }
  if (bout.winner_id === null) {
    return { kind: "draw" };
  }
  const winner = bout.fighters.find(
    (fighter) => fighter.fighter_id === bout.winner_id,
  );
  return { kind: "winner", name: winner?.name ?? DASH };
}
