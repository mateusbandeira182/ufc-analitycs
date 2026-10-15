import type { CurrentFormOut, FighterStatsOut } from "@/api/schema";
import { Skeleton } from "@/components/ui/skeleton";
import { StaleFeaturesBadge } from "@/components/StaleFeaturesBadge";
import {
  formatAvgControlTime,
  formatAverage,
  formatCount,
  formatTrend,
} from "@/features/fighters/statsFormat";
import { useFighterStats } from "@/features/fighters/useFighterStats";

/*
  Bloco FORMA ATUAL: o recorte as-of das três lutas anteriores, lido da MESMA
  tabela que alimenta a predição (`fighter_asof_features`, RF-15). Perfil e
  palpite contam a mesma história por construção, e não por dois cálculos que se
  espera que coincidam.

  A gramática visual é deliberadamente outra que a de CARREIRA: aqui não há
  octógono partido nem numeral gigante, e sim um livro-caixa de linhas em fonte
  monoespaçada, dividido em três faixas rotuladas pelo que cada uma mede. A
  separação por faixa é informação, não ornamento — "0,7 na janela" e "+0,7
  contra a carreira" são leituras diferentes da mesma métrica, e o leitor precisa
  saber qual está vendo.

  Os indicadores de tendência **não** ganham cor nem seta de bom/ruim (DP-10):
  absorver mais golpe é pior, e os outros quatro positivos são melhores. Um sinal
  gráfico uniforme mentiria em um dos cinco, e preferimos não sinalizar a
  sinalizar errado.
*/

const HEADING_ID = "fighter-current-form";
const WINDOW_GROUP_ID = "fighter-current-form-window";
const TREND_GROUP_ID = "fighter-current-form-trend";
const WEAR_GROUP_ID = "fighter-current-form-wear";

/** Uma linha do livro-caixa: o que se mede, em que unidade, e o número. */
interface FormMetric {
  label: string;
  unit: string;
  value: string;
}

/**
 * Seção FORMA ATUAL da página do lutador. Consome o mesmo `useFighterStats` da
 * seção CARREIRA (uma query, dois campos da resposta) e trata os três estados do
 * server-state. O aviso de defasagem mora aqui, na seção que lê o dado defasado
 * — nunca num banner global.
 */
export function FighterCurrentForm({ fighterId }: { fighterId: number }) {
  const { data, isPending, isError } = useFighterStats(fighterId);

  return (
    <section aria-labelledby={HEADING_ID}>
      <h2
        id={HEADING_ID}
        className="font-display text-xl font-bold uppercase tracking-wide"
      >
        Forma atual
      </h2>
      <p className="mb-5 mt-1 text-[0.7rem] uppercase tracking-widest text-muted-foreground">
        As três lutas anteriores, como o modelo as enxerga
      </p>

      {isPending ? (
        <div
          aria-busy="true"
          aria-label="Carregando forma atual"
          className="grid gap-4"
        >
          <Skeleton className="h-40 w-full" />
        </div>
      ) : isError ? (
        <p role="status" className="text-sm text-muted-foreground">
          Não foi possível carregar a forma atual deste lutador.
        </p>
      ) : (
        <CurrentFormBody stats={data} />
      )}
    </section>
  );
}

/**
 * O corpo da seção: o aviso de defasagem (quando houver) e, abaixo, as três
 * faixas de números — ou a declaração de ausência.
 *
 * `current_form` nulo tem **duas** causas distintas, e a tela as separa porque a
 * ação do leitor é diferente em cada uma: sem histórico no acervo não há o que
 * fazer; cache nunca materializado se resolve com o comando que o aviso mostra.
 */
function CurrentFormBody({ stats }: { stats: FighterStatsOut }) {
  const neverMaterialized = stats.features_freshness.materialized_at === null;

  return (
    <div className="grid gap-5">
      <StaleFeaturesBadge freshness={stats.features_freshness} />

      {stats.current_form === null ? (
        <p className="rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center text-sm text-muted-foreground">
          {neverMaterialized
            ? "Os números da forma atual aparecem aqui depois que o cache for materializado."
            : "Sem histórico suficiente no acervo para compor a forma atual deste atleta."}
        </p>
      ) : (
        <CurrentFormLedger form={stats.current_form} />
      )}
    </div>
  );
}

/** As três faixas de números as-of, cada uma rotulada pelo que mede. */
function CurrentFormLedger({ form }: { form: CurrentFormOut }) {
  return (
    <div className="grid gap-px overflow-hidden rounded-lg border border-border bg-border">
      <MetricGroup
        id={WINDOW_GROUP_ID}
        title="Janela de 3 lutas"
        metrics={[
          {
            label: "Golpes conectados",
            unit: "por minuto",
            value: formatAverage(form.sig_strikes_landed_pm_r3),
          },
          {
            label: "Golpes absorvidos",
            unit: "por minuto",
            value: formatAverage(form.sig_strikes_absorbed_pm_r3),
          },
          {
            label: "Quedas conectadas",
            unit: "por luta",
            value: formatAverage(form.takedowns_landed_avg_r3),
          },
          {
            label: "Defesa de queda",
            unit: "fração de 0 a 1",
            value: formatAverage(form.takedown_defense_r3),
          },
          {
            label: "Tempo de controle",
            unit: "por luta",
            value: formatAvgControlTime(form.control_time_avg_r3),
          },
        ]}
      />

      <MetricGroup
        id={TREND_GROUP_ID}
        title="Tendência"
        caption="Comparado à média de carreira"
        metrics={[
          {
            label: "Golpes conectados",
            unit: "por minuto",
            value: formatTrend(form.sig_strikes_landed_pm_trend),
          },
          {
            label: "Golpes absorvidos",
            unit: "por minuto",
            value: formatTrend(form.sig_strikes_absorbed_pm_trend),
          },
          {
            label: "Quedas conectadas",
            unit: "por luta",
            value: formatTrend(form.takedowns_landed_avg_trend),
          },
          {
            label: "Defesa de queda",
            unit: "fração de 0 a 1",
            value: formatTrend(form.takedown_defense_trend),
          },
          {
            label: "Tempo de controle",
            unit: "segundos por luta",
            value: formatTrend(form.control_time_avg_trend),
          },
        ]}
      />

      <MetricGroup
        id={WEAR_GROUP_ID}
        title="Desgaste acumulado"
        caption="Carreira anterior a esta luta, não a janela de 3"
        metrics={[
          {
            label: "Minutos de luta",
            unit: "acumulados",
            value: formatAverage(form.career_minutes_before),
          },
          {
            label: "Derrotas por KO/TKO",
            unit: "acumuladas",
            value: formatCount(form.ko_losses_prior),
          },
          {
            label: "Derrotas por finalização",
            unit: "acumuladas",
            value: formatCount(form.submission_losses_prior),
          },
        ]}
      />
    </div>
  );
}

/**
 * Uma faixa do livro-caixa. O rótulo da faixa é o que impede o leitor de atribuir
 * um número de carreira à janela de três lutas — a confusão que a RF-24 nomeia.
 */
function MetricGroup({
  id,
  title,
  caption,
  metrics,
}: {
  id: string;
  title: string;
  caption?: string;
  metrics: FormMetric[];
}) {
  return (
    <div role="group" aria-labelledby={id} className="bg-card p-5">
      <h3
        id={id}
        className="font-display text-sm font-bold uppercase tracking-[0.2em] text-primary"
      >
        {title}
      </h3>
      {caption === undefined ? null : (
        <p className="mt-0.5 text-[0.65rem] uppercase tracking-widest text-muted-foreground">
          {caption}
        </p>
      )}
      <dl className="mt-4 grid gap-2.5">
        {metrics.map((metric) => (
          <div
            key={metric.label}
            className="grid grid-cols-[1fr_auto] items-baseline gap-3 border-t border-border/60 pt-2.5 first:border-t-0 first:pt-0"
          >
            <dt className="text-sm text-muted-foreground">
              {metric.label}
              <span className="ml-1.5 text-[0.65rem] uppercase tracking-widest text-muted-foreground/60">
                {metric.unit}
              </span>
            </dt>
            <dd className="font-mono text-lg font-bold tabular-nums">
              {metric.value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
