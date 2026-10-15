import type { MatchupBlockInfluenceOut } from "@/api/schema";

/*
  Lógica pura da seção "o que moveu a agulha": formatação do deslocamento, lado
  favorecido, rótulo do bloco, cobertura e ordenação.

  D-11 / DT-2: `delta_prob_a = prob_a_base − prob_a_ablado`, e **positivo
  favorece o lado A**. É deliberadamente OPOSTO, em semântica, a
  `analysis.metrics.MetricsDelta` (onde `delta = log_loss(com) − log_loss(sem)` e
  negativo é bom): são grandezas diferentes — perda contra atribuição de
  probabilidade. Quem "harmonizar" os dois sinais inverte esta tela sem quebrar
  teste nenhum.

  Não reusa `formatTrend` de `@/features/fighters/statsFormat` de propósito: lá o
  sinal é cosmético (positivo/negativo de uma média), aqui ele **escolhe de quem
  é o nome exibido**. Um ajuste em `formatTrend` para a página do lutador não
  pode poder inverter o lado favorecido no confronto.
*/

/** Uma casa decimal: o número informa, a barra ranqueia. */
const DECIMAL_PLACES = 1;

/** Magnitude do deslocamento em pontos percentuais, já arredondada para exibição. */
function roundedPoints(delta: number): string {
  return Math.abs(delta * 100).toFixed(DECIMAL_PLACES);
}

/** Zero depois do arredondamento — `0,0` não sustenta afirmação de direção. */
function isFlat(delta: number): boolean {
  return Number(roundedPoints(delta)) === 0;
}

/**
 * Deslocamento de probabilidade em pontos percentuais (ex.: `0.041` ->
 * `"+4,1 p.p."`). Zero sai **sem sinal**: `"+0,0"` afirmaria uma direção que a
 * medição não tem, e `(-0).toFixed(1)` é `"-0.0"`.
 */
export function formatDeltaProbPoints(delta: number): string {
  const magnitude = roundedPoints(delta).replace(".", ",");
  if (isFlat(delta)) {
    return `${magnitude} p.p.`;
  }
  return `${delta > 0 ? "+" : "-"}${magnitude} p.p.`;
}

/**
 * Lado que o bloco favorece: positivo é o lado A, negativo é o lado B (D-11).
 * Decide pelo **mesmo valor arredondado** que `formatDeltaProbPoints` exibe, para
 * que rótulo e número nunca se contradigam na mesma linha da tela.
 */
export function favoredLabel(
  delta: number,
  nameA: string,
  nameB: string,
): string {
  if (isFlat(delta)) {
    return "sem deslocamento";
  }
  return `favorece ${delta > 0 ? nameA : nameB}`;
}

/*
  Mapa de RÓTULO, e só de rótulo (D-A): não decide presença, ordem nem cobertura
  — a tela renderiza o que `blocks` disser, entrada por entrada. Bloco ausente
  daqui cai no fallback degradado, que o deixa VISÍVEL em vez de escondê-lo. Se
  este mapa decidisse presença, um bloco novo do backend desapareceria da tela em
  silêncio.

  As onze chaves são os nomes de `BLOCK_REGISTRY` (`analysis/ablation.py`).
*/
export const BLOCK_LABELS: Record<string, string> = {
  "striking-profile": "Perfil de striking",
  "dinamica-por-round": "Dinâmica por round",
  "dimensoes-ausentes": "Dimensões ausentes",
  "divisao-categoria": "Divisão (categoria de peso)",
  "divisao-taxa-base": "Divisão (taxa de finalização)",
  formato: "Formato da luta",
  "historico-qualificado": "Histórico qualificado",
  "confronto-de-bases": "Confronto de bases",
  "eixos-de-estilo": "Eixos de estilo",
  "estilo-semelhante": "Desempenho contra estilo semelhante",
  "forca-de-calendario": "Força de calendário",
};

/**
 * Rótulo legível do bloco. Nome não mapeado degrada para o slug humanizado
 * (hífens por espaço, inicial maiúscula) — deliberadamente visível, porque um
 * bloco que ninguém rotulou tem de se notar na tela. Nome vazio vira um aviso,
 * nunca string vazia: um item em branco na lista não se explica.
 */
export function blockLabel(name: string): string {
  const mapped = BLOCK_LABELS[name];
  if (mapped !== undefined) {
    return mapped;
  }
  const humanized = name.replaceAll("-", " ").trim();
  if (humanized === "") {
    return "Bloco sem nome";
  }
  return humanized.charAt(0).toLocaleUpperCase("pt-BR") + humanized.slice(1);
}

/**
 * Bloco sem medição: `coverage: "none"` **ou** deslocamento ausente. Predicado
 * único compartilhado por rótulo, barra e ordenação, para que os três nunca
 * discordem — `null` é "não avaliado", jamais zero, e desenhar barra de largura
 * zero ainda é desenhar uma barra onde não houve medição.
 */
export function isUnevaluated(block: MatchupBlockInfluenceOut): boolean {
  return block.coverage === "none" || block.delta_prob_a === null;
}

/** "coluna" ou "colunas", conforme a contagem. */
function colunas(n: number): string {
  return n === 1 ? "1 coluna avaliada" : `${String(n)} colunas avaliadas`;
}

/**
 * Texto da cobertura, derivado das **contagens do payload** — nunca de lista
 * fixa de blocos avaliáveis (RF-18). A cobertura varia por par: a Sprint 05
 * mediu `dinamica-por-round` saindo `none` num par real, fora da lista que a
 * SPEC antecipava. Lista fixa apodreceria exatamente como o cache
 * `bout_features` apodreceu.
 */
export function coverageLabel(block: MatchupBlockInfluenceOut): string {
  if (block.coverage === "none") {
    return "não avaliado";
  }
  if (block.coverage === "partial") {
    return `parcial: ${String(block.n_columns_evaluated)} de ${String(block.n_columns)} colunas avaliadas`;
  }
  return colunas(block.n_columns_evaluated);
}

/**
 * Ordem de exibição: `|delta|` decrescente, blocos sem medição **sempre no fim**
 * na ordem da resposta, empate resolvido pela ordem da resposta (`sort` estável).
 *
 * `none` no meio do ranking leria como "influência zero", que é a leitura
 * proibida: zero medido e não medido são estados distintos, e posição é
 * informação. Não muta a lista recebida.
 */
export function sortedBlocks(
  blocks: MatchupBlockInfluenceOut[],
): MatchupBlockInfluenceOut[] {
  return [...blocks].sort((left, right) => {
    const leftOut = isUnevaluated(left);
    const rightOut = isUnevaluated(right);
    if (leftOut !== rightOut) {
      return leftOut ? 1 : -1;
    }
    if (leftOut) {
      return 0;
    }
    return Math.abs(right.delta_prob_a ?? 0) - Math.abs(left.delta_prob_a ?? 0);
  });
}

/**
 * Maior `|delta|` entre os blocos avaliados: a barra **ranqueia**, o número
 * informa. Zero quando nenhum bloco foi avaliado — nesse caso não há barra a
 * escalar.
 */
export function barScaleOf(blocks: MatchupBlockInfluenceOut[]): number {
  return blocks.reduce(
    (maior, entry) =>
      isUnevaluated(entry)
        ? maior
        : Math.max(maior, Math.abs(entry.delta_prob_a ?? 0)),
    0,
  );
}
