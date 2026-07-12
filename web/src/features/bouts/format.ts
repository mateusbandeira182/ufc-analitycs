import type { BoutDetailOut } from "@/api/schema";
import { DASH } from "@/lib/format";

/*
  Formatadores específicos da página da luta. Os genéricos (data, método, tempo de
  encerramento com round) vivem em `@/lib/format`; aqui ficam os de apresentação
  do box-score granular — duração pura mm:ss e a resolução do vencedor por nome.
  Promover para `@/lib/format` só quando uma segunda feature precisar.
*/

/**
 * Duração em segundos como `m:ss` (ex.: `315` -> `"5:15"`). Usada tanto no tempo
 * de encerramento quanto no tempo de controle — ambos são segundos inteiros no
 * contrato (decisão de schema; a formatação é apresentação). Traço quando nula.
 */
export function formatDuration(totalSeconds: number | null): string {
  if (totalSeconds === null) {
    return DASH;
  }
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes)}:${String(seconds).padStart(2, "0")}`;
}

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
