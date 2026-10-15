/**
 * A invariante de grade compartilhada pelas três telas que desenham cantos.
 *
 * `HomePage`, `EventBoutCard` e `BoutDetail` usam o mesmo vernáculo — dois
 * cantos lado a lado, vermelho à esquerda — e declaram o template de colunas
 * **separadamente**. Em agosto de 2026 a Home declarava `[1fr_auto_1fr]` e
 * renderizava só dois filhos: o auto-placement do CSS Grid pôs o canto azul na
 * coluna `auto`, que encolhe para o conteúdo, e deixou a terceira `1fr` vazia.
 * O azul saía mais estreito que o vermelho, e **nenhum dos 401 testes pegou** —
 * eles afirmam texto, papéis e links, e o jsdom não computa layout de grid.
 *
 * As outras duas são hoje **mais** frágeis do que a Home era: a contagem de
 * filhos delas fecha por um ternário dentro de um `.map()`
 * (`index > 0 ? divisor : null`), não por um número estático. Quem mexer naquele
 * ternário reproduz o defeito, com o jsdom igualmente cego.
 *
 * Daí a asserção ser da **invariante** e não do literal: colunas declaradas ==
 * filhos da grade. Ela sobrevive a um refactor que acrescente ou remova divisor,
 * e falha para quem mexer num lado sem mexer no outro.
 */
export function contarColunas(className: string): number {
  const template = /grid-cols-\[([^\]]+)\]/.exec(className)?.[1];
  if (template !== undefined) {
    return template.split("_").length;
  }
  const quantas = /grid-cols-(\d+)/.exec(className)?.[1];
  return quantas === undefined ? 0 : Number(quantas);
}

/** A grade de cantos dentro de `escopo`, para a asserção da invariante. */
export function gradeDeCantos(escopo: HTMLElement): HTMLElement | null {
  return escopo.querySelector<HTMLElement>("[class*='grid-cols-']");
}
