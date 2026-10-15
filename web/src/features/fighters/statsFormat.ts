import type { BoutMethod } from "@/api/schema";
import { DASH, formatDuration, formatMethod } from "@/lib/format";

/*
  Formatadores das estatísticas agregadas do lutador (médias computadas on demand
  pelo backend). O tempo de controle reaproveita o m:ss genérico de `@/lib/format`;
  as médias ganham uma casa decimal em pt-BR (vírgula), preservando o zero.
*/

/**
 * Média com uma casa decimal em pt-BR (ex.: `62.53` -> `"62,5"`). Formata pela
 * string (`toFixed` + troca do ponto pela vírgula) para não depender do ICU do
 * ambiente. Traço quando a média é nula (sem valor a agregar).
 */
export function formatAverage(value: number | null): string {
  return value === null ? DASH : value.toFixed(1).replace(".", ",");
}

/**
 * Média de tempo de controle (segundos, possivelmente fracionária) como `m:ss`,
 * arredondando ao segundo antes de formatar. Traço quando nula.
 */
export function formatAvgControlTime(seconds: number | null): string {
  return seconds === null ? DASH : formatDuration(Math.round(seconds));
}

/**
 * Tendência assinada com uma casa decimal em pt-BR (`+2,1`, `-0,4`, `0,0`). O
 * sinal explícito é o que torna o número legível como comparação com a média de
 * carreira, e não como medida absoluta.
 *
 * Zero é medição legítima — a janela de três lutas coincidindo com a carreira
 * inteira —, então nunca colapsa em ausência; `null` é ausência e vira traço. O
 * sinal **não** é traduzido em juízo de bom/ruim: em golpes absorvidos, positivo
 * é pior, e nos outros quatro é melhor.
 */
export function formatTrend(value: number | null): string {
  if (value === null) {
    return DASH;
  }
  const texto = value.toFixed(1).replace(".", ",");
  // `(-0.2).toFixed(1)` já traz o sinal; só o positivo precisa ganhá-lo. O zero
  // fica sem sinal — "+0,0" afirmaria uma direção que a medição não tem. E
  // `(-0).toFixed(1)` é "-0.0", que viraria um zero negativo na tela.
  if (texto.startsWith("-")) {
    return Number(value) === 0 ? "0,0" : texto;
  }
  return value > 0 ? `+${texto}` : texto;
}

/**
 * Contagem inteira (ex.: derrotas por nocaute na carreira anterior). Os valores
 * chegam como `float` do payload JSONB, e `1,0 derrota` leria como média de algo
 * — contagem é contagem. Zero preserva-se: ter histórico e nunca ter perdido
 * daquele jeito é uma medição; ausência vira traço.
 */
export function formatCount(value: number | null): string {
  return value === null ? DASH : String(Math.round(value));
}

/** Rótulo explícito para share de striking sem dado (denominador zero no backend). */
export const NO_DATA = "sem dado";

/**
 * Formata uma fração de golpes conectados (0..1) como percentual inteiro em pt-BR
 * (ex.: `0.453` -> `"45%"`). O share vem do backend como número ou nulo; nulo — e
 * qualquer valor não finito, por garantia — vira "sem dado" (nunca NaN/Infinity
 * na tela).
 */
export function formatShare(value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return NO_DATA;
  }
  return `${String(Math.round(value * 100))}%`;
}

/** Um método de encerramento com sua contagem de vitórias, já rotulado. */
export interface FinishSegment {
  method: BoutMethod;
  label: string;
  count: number;
}

// Ordem canônica de exibição dos métodos (do mais explosivo ao mais técnico).
const METHOD_ORDER: BoutMethod[] = [
  "ko_tko",
  "submission",
  "decision",
  "dq",
  "no_contest",
];

/**
 * Converte o mapa `wins_by_method` do backend em segmentos ordenados, mantendo
 * apenas os métodos com ao menos uma vitória — a base da barra "Como venceu".
 */
export function finishSegments(
  winsByMethod: Record<string, number>,
): FinishSegment[] {
  return METHOD_ORDER.map((method) => ({
    method,
    label: formatMethod(method),
    count: winsByMethod[method] ?? 0,
  })).filter((segment) => segment.count > 0);
}
