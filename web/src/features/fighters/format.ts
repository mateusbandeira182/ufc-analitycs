import type { BoutMethod, FighterOut, Stance } from "@/api/schema";
import { DASH } from "@/lib/format";

/*
  Formatadores específicos do lutador. Os genéricos (data, método, tempo de
  encerramento) vivem em `@/lib/format` e são reexportados aqui para preservar os
  call sites e testes existentes desta feature.
*/
export { formatEndingTime, formatIsoDate, formatMethod } from "@/lib/format";

/** Cartel no formato "V-D-E" (vitórias-derrotas-empates). */
export function formatRecord(fighter: FighterOut): string {
  return `${String(fighter.wins)}-${String(fighter.losses)}-${String(fighter.draws)}`;
}

const STANCE_LABELS: Record<Stance, string> = {
  orthodox: "Ortodoxa",
  southpaw: "Canhota",
  switch: "Alternada",
};

/** Rótulo em pt-BR da base (guarda) do lutador; traço quando desconhecida. */
export function formatStance(stance: Stance | null): string {
  return stance ? STANCE_LABELS[stance] : DASH;
}

/**
 * Rótulo do resultado da luta pela ótica do lutador. `won` é booleano derivado
 * pelo backend; empate e no contest chegam como `won=false`. A linha só
 * distingue "Sem resultado" (no contest); o cartel V/D/E do topo é a fonte
 * autoritativa do retrospecto — a linha não infere empate (o dado não vem).
 */
export function formatResult(bout: {
  won: boolean;
  method: BoutMethod;
}): string {
  if (bout.won) {
    return "Vitória";
  }
  return bout.method === "no_contest" ? "Sem resultado" : "Derrota";
}

/** Altura em centímetros; traço quando ausente. */
export function formatHeight(heightCm: number | null): string {
  return heightCm === null ? DASH : `${String(heightCm)} cm`;
}

/** Alcance em centímetros; traço quando ausente. */
export function formatReach(reachCm: number | null): string {
  return reachCm === null ? DASH : `${String(reachCm)} cm`;
}

/**
 * Peso em quilos; traço quando ausente. Arredonda a uma casa decimal e a exibe em
 * pt-BR (vírgula), omitindo a casa quando o valor é inteiro (ex.: `93` -> `"93 kg"`,
 * `70.3` -> `"70,3 kg"`).
 */
export function formatWeight(weightKg: number | null): string {
  if (weightKg === null) {
    return DASH;
  }
  const rounded = Math.round(weightKg * 10) / 10;
  const text = Number.isInteger(rounded)
    ? String(rounded)
    : rounded.toFixed(1).replace(".", ",");
  return `${text} kg`;
}
