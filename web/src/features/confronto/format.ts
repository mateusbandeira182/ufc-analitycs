import { DASH } from "@/lib/format";

/*
  Formatadores específicos da página de confronto. `clampProbability` e
  `formatProbability` foram promovidos para `@/lib/format` quando o card de
  evento futuro passou a precisar deles; reexportados aqui para preservar os call
  sites e os testes desta feature, como `formatDuration` já é em `bouts/format`.
*/
export { clampProbability, formatProbability } from "@/lib/format";

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
