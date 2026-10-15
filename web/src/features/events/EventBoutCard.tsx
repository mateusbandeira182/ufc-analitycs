import { ArrowRight, HelpCircle, Trophy } from "lucide-react";
import { Fragment } from "react";
import { Link } from "react-router";

import type {
  BoutCardFighterOut,
  BoutCardOut,
  CardBoutPredictionOut,
} from "@/api/schema";
import { UnavailablePrediction } from "@/components/UnavailablePrediction";
import { Avatar } from "@/components/ui/avatar";
import { Card } from "@/components/ui/card";
import {
  clampProbability,
  formatEndingTime,
  formatMethod,
  formatProbability,
} from "@/lib/format";

interface EventBoutCardProps {
  bout: BoutCardOut;
  /**
   * Previsão desta luta, indexada por `bout_id` pela página. União com
   * `undefined` e **não** prop opcional: `exactOptionalPropertyTypes` está
   * ligado e distingue "propriedade ausente" de "presente com `undefined`", o
   * que faria a forma opcional recusar `prediction={porLuta.get(bout.id)}`.
   *
   * `undefined` é o estado normal de um card realizado (a requisição nem sai) e
   * de um card agendado cuja previsão ainda carrega ou falhou.
   */
  prediction: CardBoutPredictionOut | undefined;
}

/**
 * Card de uma luta do evento: os dois cantos como "A vs B", com dois modos.
 *
 * O modo vem do **`status` da luta** (RF-31 / D-14), nunca da data do evento nem
 * da ausência de `method`: a janela entre o evento acontecer e alguém rodar o
 * `gap_sync` (manual) dura dias, e uma luta realizada de método desconhecido
 * sairia "Agendada" pelo proxy. Decidir por luta também resolve o card **misto**
 * sem regra extra.
 *
 * Realizada: vencedor destacado (Trophy + ouro do cinturão) e o resultado — o
 * comportamento de sempre. Agendada: nenhum canto rotulado vencedor, o palpite
 * do modelo e o link que o explica.
 */
export function EventBoutCard({ bout, prediction }: EventBoutCardProps) {
  // Ordena por canto (vermelho primeiro) para exibição determinística — não
  // confia na ordem do array da resposta.
  const ordered = [...bout.fighters].sort((a, b) =>
    a.corner === b.corner ? 0 : a.corner === "red" ? -1 : 1,
  );
  const matchup = ordered.map((fighter) => fighter.name).join(" vs ");
  const scheduled = bout.status === "scheduled";

  return (
    <li aria-label={matchup}>
      <Card className="overflow-hidden p-0">
        <div className="grid grid-cols-[1fr_auto_1fr] items-stretch">
          {ordered.map((fighter, index) => (
            <Fragment key={fighter.fighter_id}>
              {index > 0 ? <VersusDivider /> : null}
              <FighterCorner
                fighter={fighter}
                isWinner={bout.winner_id === fighter.fighter_id}
              />
            </Fragment>
          ))}
        </div>

        {scheduled && prediction ? (
          <PredictionBand bout={bout} prediction={prediction} />
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border bg-secondary/40 px-4 py-2.5">
          {scheduled ? (
            <ScheduledMeta bout={bout} />
          ) : (
            <ResultMeta bout={bout} />
          )}
          <Link
            to={`/bouts/${String(bout.id)}`}
            className="inline-flex items-center gap-1 text-xs font-medium uppercase tracking-wide text-muted-foreground transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Ver luta
            <ArrowRight aria-hidden="true" className="size-3.5" />
          </Link>
        </div>
      </Card>
    </li>
  );
}

/** Método, momento do encerramento e divisão — a luta que já aconteceu. */
function ResultMeta({ bout }: { bout: BoutCardOut }) {
  return (
    <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[0.7rem] uppercase tracking-widest text-muted-foreground">
      <span className="text-foreground">{formatMethod(bout.method)}</span>
      <span aria-hidden="true">·</span>
      <span>{formatEndingTime(bout.round, bout.ending_time_seconds)}</span>
      {bout.weight_class ? (
        <>
          <span aria-hidden="true">·</span>
          <span>{bout.weight_class}</span>
        </>
      ) : null}
    </p>
  );
}

/**
 * Rodapé da luta agendada: o rótulo do estado e a divisão, quando houver.
 *
 * Não chama `formatEndingTime(null, null)`: o traço que ele devolve é a resposta
 * para "decisão sem tempo de parada registrado", não para "luta que não
 * aconteceu". E o rótulo é literal, decidido pelo `status` — `formatMethod(null)`
 * devolveria o mesmo texto por um proxy.
 */
function ScheduledMeta({ bout }: { bout: BoutCardOut }) {
  return (
    <p className="font-mono text-[0.7rem] uppercase tracking-widest text-foreground">
      Agendada
      {bout.weight_class ? (
        <span className="text-muted-foreground">
          {" · "}
          {bout.weight_class}
        </span>
      ) : null}
    </p>
  );
}

/**
 * A faixa do palpite, só na luta agendada. Dois estados, de formas
 * **diferentes**: com previsão, a barra dividida; sem previsão, o aviso da causa
 * e nenhuma barra.
 *
 * A diferença de forma é a decisão de desenho que sustenta a RF-21: uma barra
 * parada no meio se leria como opinião do modelo quando é ausência dela, então o
 * estado indisponível não tem barra nenhuma — não há como confundir "não sei"
 * com "está igual".
 *
 * Indisponível é `unavailable_code !== null` **ou** qualquer probabilidade nula:
 * `clampProbability(null)` renderizaria "0%", uma afirmação sobre uma luta que
 * ninguém mediu.
 */
function PredictionBand({
  bout,
  prediction,
}: {
  bout: BoutCardOut;
  prediction: CardBoutPredictionOut;
}) {
  const href = confrontoHref(bout);
  const red = prediction.fighter_red;
  const blue = prediction.fighter_blue;
  const probRed = prediction.prob_red_wins;
  const probBlue = prediction.prob_blue_wins;

  if (
    prediction.unavailable_code !== null ||
    probRed === null ||
    probBlue === null ||
    red === null ||
    blue === null
  ) {
    return (
      <div>
        <UnavailablePrediction
          code={prediction.unavailable_code}
          reason={prediction.unavailable_reason}
          variant="inline"
        />
        {href ? <WhyLink href={href} className="px-4 pb-3 pt-2" /> : null}
      </div>
    );
  }

  // O favorito sai do `predicted_winner_id` cruzado com o **id** do canto. Nome
  // não é chave: um `Map` por nome foi o defeito que fez toda luta de adversário
  // comum sair "Perdeu" na Sprint 08.
  const favoredIsRed = prediction.predicted_winner_id === red.id;
  const favored = favoredIsRed ? red : blue;
  const favoredProb = favoredIsRed ? probRed : probBlue;

  return (
    <div className="border-t border-dashed border-border px-4 py-3">
      <div role="group" aria-label="Probabilidade de vitória">
        <p className="font-display text-[0.6rem] font-medium uppercase tracking-[0.25em] text-primary">
          Palpite do modelo
        </p>
        <div className="mt-1.5 flex items-baseline justify-between gap-3">
          <span className="min-w-0 truncate font-display text-sm font-semibold uppercase tracking-wide">
            {favored.name}
          </span>
          {/* O favorito previsto NUNCA usa o ouro do cinturão: naquele vernáculo
              o ouro já significa "venceu", e um palpite não é um resultado. */}
          <span className="font-mono text-sm font-bold tabular-nums text-primary">
            {formatProbability(favoredProb)}
          </span>
        </div>
        <OddsSplit probRed={probRed} />
      </div>
      {href ? <WhyLink href={href} className="pt-2.5" /> : null}
    </div>
  );
}

/**
 * Trilha única repartida entre os dois cantos, encontrando-se no número do
 * modelo — o quadro de odds da mesa de apostas, que é o vernáculo do card.
 *
 * Uma trilha em vez das duas barras empilhadas da página de confronto: num card
 * de treze lutas seriam vinte e seis barras, e a página passaria a ser sobre o
 * modelo em vez de sobre as lutas. O canto azul ocupa o resto por `flex-1`, o
 * que torna a complementaridade uma propriedade do layout — sem folga de
 * arredondamento entre as duas.
 *
 * `aria-hidden` porque o texto acima já carrega a informação inteira.
 */
function OddsSplit({ probRed }: { probRed: number }) {
  const width = clampProbability(probRed) * 100;

  return (
    <div
      aria-hidden="true"
      className="mt-2 flex h-1.5 w-full overflow-hidden rounded-full"
    >
      <span
        className="h-full bg-corner-red"
        style={{ width: `${String(width)}%` }}
      />
      <span className="h-full flex-1 bg-corner-blue" />
    </div>
  );
}

/** O link que explica o palpite, na página de confronto dos dois atletas. */
function WhyLink({ href, className }: { href: string; className: string }) {
  return (
    <Link
      to={href}
      className={`inline-flex items-center gap-1 text-xs font-medium uppercase tracking-wide text-muted-foreground transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${className}`}
    >
      <HelpCircle aria-hidden="true" className="size-3.5" />
      Por que?
    </Link>
  );
}

/**
 * `/confronto?a=&b=` montado pelos **ids** dos dois cantos do card. `null`
 * quando a luta não tem um atleta no vermelho e um no azul — é exatamente o que
 * o código `missing_corners` relata, e `/confronto` exige dois lutadores: um
 * link quebrado seria pior que a ausência dele.
 */
function confrontoHref(bout: BoutCardOut): string | null {
  const red = bout.fighters.find((fighter) => fighter.corner === "red");
  const blue = bout.fighters.find((fighter) => fighter.corner === "blue");
  if (!red || !blue) {
    return null;
  }
  return `/confronto?a=${String(red.fighter_id)}&b=${String(blue.fighter_id)}`;
}

function FighterCorner({
  fighter,
  isWinner,
}: {
  fighter: BoutCardFighterOut;
  isWinner: boolean;
}) {
  const cornerAccent =
    fighter.corner === "red" ? "bg-corner-red" : "bg-corner-blue";

  return (
    <div className="relative flex items-start gap-3 p-4">
      {/* Faixa do canto: vermelho ou azul, o vernáculo do octógono. */}
      <span
        aria-hidden="true"
        className={`absolute inset-x-0 top-0 h-1 ${cornerAccent}`}
      />
      {/* Retrato no recorte do octógono; iniciais quando não há `headshot_url`,
          que é o caso comum do acervo. */}
      <Avatar
        name={fighter.name}
        src={fighter.headshot_url}
        className="mt-0.5 size-10"
      />
      <div className="flex min-w-0 flex-col gap-1">
        <span className="text-[0.6rem] font-medium uppercase tracking-[0.2em] text-muted-foreground">
          {fighter.corner === "red" ? "Canto vermelho" : "Canto azul"}
        </span>
        <Link
          to={`/fighters/${String(fighter.fighter_id)}`}
          aria-label={isWinner ? `${fighter.name}, vencedor` : undefined}
          className="font-display text-base font-semibold uppercase tracking-wide hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {fighter.name}
        </Link>
        {isWinner ? (
          <span
            aria-hidden="true"
            className="inline-flex w-fit items-center gap-1 rounded-sm bg-belt-gold/15 px-1.5 py-0.5 text-[0.6rem] font-bold uppercase tracking-widest text-belt-gold"
          >
            <Trophy className="size-3" />
            Vencedor
          </span>
        ) : null}
      </div>
    </div>
  );
}

function VersusDivider() {
  return (
    <div className="flex items-center justify-center px-2">
      <span className="font-display text-xs font-bold uppercase tracking-widest text-primary">
        vs
      </span>
    </div>
  );
}
