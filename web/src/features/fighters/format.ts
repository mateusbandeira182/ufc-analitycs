import type { FighterOut, Stance } from "@/api/schema";

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
  return stance ? STANCE_LABELS[stance] : "—";
}
