/*
  Formatação da probabilidade do palpite (0..1) como percentual inteiro.
  O backend garante `prob_a_wins + prob_b_wins == 1`, mas a tela nunca confia
  cegamente: valor não finito ou fora de [0,1] é saneado antes de virar texto ou
  largura de barra — nunca NaN/Infinity na tela.
*/

/** Fração saneada para [0,1]; não finito vira 0. Base comum de texto e barra. */
export function clampProbability(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.min(1, Math.max(0, value));
}

/** Fração (0..1) como percentual inteiro (ex.: `0.5094` -> `"51%"`). */
export function formatProbability(value: number): string {
  return `${String(Math.round(clampProbability(value) * 100))}%`;
}
