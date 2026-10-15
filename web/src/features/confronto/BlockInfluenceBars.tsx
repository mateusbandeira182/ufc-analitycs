import type { MatchupBlockInfluenceOut } from "@/api/schema";
import {
  barScaleOf,
  blockLabel,
  coverageLabel,
  favoredLabel,
  formatDeltaProbPoints,
  isUnevaluated,
  sortedBlocks,
} from "@/features/confronto/blockInfluence";

interface BlockInfluenceBarsProps {
  /** Blocos como a resposta os trouxe — a tela renderiza o que vier, sem lista fixa. */
  blocks: MatchupBlockInfluenceOut[];
  /** Nome do lado A: é dele que sai "favorece ..." quando `delta_prob_a` é positivo (D-11). */
  nameA: string;
  nameB: string;
}

/**
 * "O que moveu a agulha": a influência de cada bloco de features sobre a
 * probabilidade do confronto, medida por ablação local.
 *
 * A barra é **divergente a partir do eixo central**, com os cantos do octógono
 * como direção: o lado B à esquerda em azul, o lado A à direita em vermelho.
 * Assim o sinal de `delta_prob_a` deixa de ser um `+`/`-` decorativo e vira
 * posição e cor — a informação que o sinal carrega é justamente de que lado a
 * agulha andou. A legenda do eixo nomeia os dois lados; sem ela, "a barra foi
 * para a direita" não diria nada.
 *
 * A barra é `aria-hidden`: o texto (deslocamento em pontos percentuais e lado
 * favorecido) carrega a informação, e a barra apenas ranqueia.
 *
 * Presentacional puro: recebe os blocos já carregados, sem server-state próprio.
 * Nenhum caminho decide se um bloco tem cobertura — a cobertura vem do payload,
 * bloco por bloco, porque ela **varia por par**.
 */
export function BlockInfluenceBars({
  blocks,
  nameA,
  nameB,
}: BlockInfluenceBarsProps) {
  if (blocks.length === 0) {
    return null;
  }

  const scale = barScaleOf(blocks);

  return (
    <section aria-labelledby="influencia-heading">
      <h2
        id="influencia-heading"
        className="font-display text-xl font-bold uppercase tracking-wide"
      >
        O que moveu a agulha
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Quanto a probabilidade do confronto se desloca quando cada divisão do
        jogo é removida do modelo.
      </p>

      <div
        className="mt-5 grid grid-cols-2 items-baseline gap-2 border-b border-border pb-2 font-display text-[0.65rem] font-semibold uppercase tracking-[0.2em]"
        aria-hidden="true"
      >
        <span className="truncate text-corner-blue">&larr; {nameB}</span>
        <span className="truncate text-right text-corner-red">
          {nameA} &rarr;
        </span>
      </div>

      <ul className="divide-y divide-border">
        {sortedBlocks(blocks).map((block) => (
          <BlockRow
            key={block.name}
            block={block}
            scale={scale}
            nameA={nameA}
            nameB={nameB}
          />
        ))}
      </ul>
    </section>
  );
}

/**
 * Uma divisão do jogo. Três aparências, deliberadamente inconfundíveis entre si:
 *
 * - **deslocou**: barra sólida saindo do eixo para o lado favorecido, na cor
 *   daquele canto;
 * - **zero medido**: trilho sólido com o marcador **no** eixo — mediu e pousou
 *   no meio;
 * - **não avaliado**: trilho tracejado e vazio, sem barra e sem marcador. O
 *   tracejado já é o vocabulário de "nada aqui" no resto da SPA, e ausência de
 *   medição não pode parecer medição pequena.
 */
function BlockRow({
  block,
  scale,
  nameA,
  nameB,
}: {
  block: MatchupBlockInfluenceOut;
  scale: number;
  nameA: string;
  nameB: string;
}) {
  const label = blockLabel(block.name);
  const unevaluated = isUnevaluated(block);
  const delta = block.delta_prob_a;

  return (
    <li aria-label={label} className="grid gap-1.5 py-3.5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5">
        <p className="font-display text-sm font-semibold uppercase tracking-wide">
          {label}
        </p>
        {unevaluated || delta === null ? null : (
          <p className="font-mono text-sm font-bold tabular-nums">
            {formatDeltaProbPoints(delta)}
          </p>
        )}
      </div>

      {unevaluated || delta === null ? (
        <EmptyTrack />
      ) : (
        <DivergingBar delta={delta} scale={scale} />
      )}

      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 text-xs text-muted-foreground">
        <span>{coverageLabel(block)}</span>
        {unevaluated || delta === null ? null : (
          <span>{favoredLabel(delta, nameA, nameB)}</span>
        )}
      </div>
    </li>
  );
}

/**
 * Barra divergente do eixo central. A largura é proporcional ao maior
 * deslocamento avaliado da resposta (a barra ranqueia; o número informa), com um
 * mínimo visível para que o zero **medido** apareça como marcador no eixo em vez
 * de desaparecer — zero medido é informação, e sumir o faria ler como ausência.
 */
function DivergingBar({ delta, scale }: { delta: number; scale: number }) {
  const ratio = scale > 0 ? Math.abs(delta) / scale : 0;
  const width = Math.max(ratio * 50, 0.6);
  const favorsA = delta > 0;

  return (
    <div className="relative h-2.5 w-full overflow-hidden rounded-full border border-border bg-card">
      <span
        aria-hidden="true"
        className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-border"
      />
      <span
        aria-hidden="true"
        data-influence-bar=""
        className={
          favorsA
            ? "absolute inset-y-0 left-1/2 rounded-r-full bg-corner-red"
            : "absolute inset-y-0 right-1/2 rounded-l-full bg-corner-blue"
        }
        style={{ width: `${String(width)}%` }}
      />
    </div>
  );
}

/**
 * Trilho de um bloco que não foi avaliado: tracejado e **vazio**. Nenhuma barra,
 * nem de largura zero — desenhá-la afirmaria visualmente uma medição que não
 * houve.
 */
function EmptyTrack() {
  return (
    <div
      aria-hidden="true"
      className="h-2.5 w-full rounded-full border border-dashed border-border bg-transparent"
    />
  );
}
