import { DASH } from "@/lib/format";

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

/**
 * Atributo as-of **cru** (RF-19) com duas casas em pt-BR. Formatado pela string
 * (`toFixed` + troca do ponto pela vírgula) para não depender do ICU do
 * ambiente — o mesmo motivo de `formatAverage`.
 *
 * Duas casas, e não uma: as oito chaves misturam taxa em [0,1]
 * (`win_rate_prior`), taxa por minuto e segundos, e uma casa só colapsaria
 * `0,675` em `0,7`. Um mapa chave -> unidade seria lista fixa que apodrece no dia
 * em que o backend acrescentar o nono atributo — o `label` do payload já carrega
 * a unidade (ex.: "... (segundos)").
 *
 * `null` é ausência de medição e vira traço; `0` é taxa legítima e se preserva.
 */
export function formatAttributeValue(value: number | null): string {
  return value === null ? DASH : value.toFixed(2).replace(".", ",");
}
