import { finishSegments } from "@/features/fighters/statsFormat";

interface FighterFinishBreakdownProps {
  winsByMethod: Record<string, number>;
  /**
   * Título da seção. É prop porque a página tem **dois** recortes concretos do
   * mesmo desenho — as vitórias de carreira e as do recorte de últimas N lutas —
   * e um título fixo faria o leitor atribuir os números ao conjunto errado.
   */
  title: string;
  /**
   * Nível do título. Os dois callsites vivem em profundidades diferentes da
   * página: o de carreira é seção irmã de CARREIRA e FORMA ATUAL (`h2`), o do
   * recorte vive **dentro** da seção do recorte (`h3`). Fixar o nível quebraria
   * a hierarquia em um dos dois, e é por ela que leitor de tela navega.
   */
  headingLevel: 2 | 3;
}

/**
 * "Como vence": as vitórias por método como uma barra segmentada (a proporção de
 * cada finalização) mais uma legenda com rótulo e contagem — a informação não
 * depende só da cor. Quando não há vitórias, uma nota substitui a barra.
 */
export function FighterFinishBreakdown({
  winsByMethod,
  title,
  headingLevel,
}: FighterFinishBreakdownProps) {
  const segments = finishSegments(winsByMethod);
  const total = segments.reduce((sum, segment) => sum + segment.count, 0);
  const Heading = headingLevel === 2 ? "h2" : "h3";

  return (
    <section>
      <Heading
        className={
          headingLevel === 2
            ? "mb-4 font-display text-xl font-bold uppercase tracking-wide"
            : "mb-4 font-display text-sm font-bold uppercase tracking-[0.2em] text-primary"
        }
      >
        {title}
      </Heading>

      {total === 0 ? (
        <p role="status" className="text-sm text-muted-foreground">
          Nenhuma vitória registrada no acervo.
        </p>
      ) : (
        <>
          <div
            aria-hidden="true"
            className="flex h-3 w-full overflow-hidden rounded-full border border-border"
          >
            {segments.map((segment, index) => (
              <span
                key={segment.method}
                className="h-full bg-belt-gold"
                style={{
                  width: `${String((segment.count / total) * 100)}%`,
                  opacity: 1 - index * 0.22,
                }}
              />
            ))}
          </div>

          <ul aria-label={title} className="mt-4 grid gap-2 sm:grid-cols-3">
            {segments.map((segment, index) => (
              <li
                key={segment.method}
                className="flex items-baseline justify-between gap-3 border-t border-border pt-2"
              >
                <span className="flex items-center gap-2 text-sm text-muted-foreground">
                  <span
                    aria-hidden="true"
                    className="size-2.5 rounded-full bg-belt-gold"
                    style={{ opacity: 1 - index * 0.22 }}
                  />
                  {segment.label}
                </span>
                <span className="font-mono text-lg font-bold tabular-nums text-belt-gold">
                  {segment.count}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
