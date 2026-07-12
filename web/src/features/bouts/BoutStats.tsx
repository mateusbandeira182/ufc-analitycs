import { Trophy } from "lucide-react";
import { Link } from "react-router";

import type { BoutFighterStatsOut } from "@/api/schema";
import {
  formatAttempts,
  formatDuration,
  formatStat,
} from "@/features/bouts/format";

interface BoutStatsProps {
  fighters: BoutFighterStatsOut[];
  winnerId: number | null;
}

/*
  Cada métrica do box-score vira uma linha com o rótulo à esquerda e o valor de
  cada canto em colunas — o formato mais direto para comparar os dois lutadores.
  A configuração declarativa evita repetir "rótulo + valor" métrica a métrica.
*/
interface Metric {
  label: string;
  hint?: string;
  value: (stats: BoutFighterStatsOut) => string;
}

const METRICS: Metric[] = [
  { label: "Knockdowns", value: (s) => formatStat(s.knockdowns) },
  {
    label: "Golpes significativos",
    hint: "acertados de tentados",
    value: (s) => formatAttempts(s.sig_strikes_landed, s.sig_strikes_attempted),
  },
  {
    label: "Quedas",
    hint: "acertadas de tentadas",
    value: (s) => formatAttempts(s.takedowns_landed, s.takedowns_attempted),
  },
  {
    label: "Tentativas de finalização",
    value: (s) => formatStat(s.submission_attempts),
  },
  {
    label: "Tempo de controle",
    value: (s) => formatDuration(s.control_time_seconds),
  },
];

/**
 * Tabela comparativa do box-score: uma coluna por canto (vermelho primeiro), uma
 * linha por métrica. O canto vencedor é destacado com o ouro do cinturão e um
 * rótulo textual — o destaque não depende só da cor. Cada nome linka para o
 * detalhe do lutador.
 */
export function BoutStats({ fighters, winnerId }: BoutStatsProps) {
  // Ordena por canto (vermelho primeiro) para exibição determinística — não
  // confia na ordem do array da resposta.
  const ordered = [...fighters].sort((a, b) =>
    a.corner === b.corner ? 0 : a.corner === "red" ? -1 : 1,
  );

  return (
    <table className="w-full border-collapse text-sm">
      <caption className="mb-4 text-left font-display text-xl font-bold uppercase tracking-wide">
        Estatísticas por lutador
      </caption>
      <thead>
        <tr>
          <td className="w-1/3" />
          {ordered.map((fighter) => (
            <CornerHeader
              key={fighter.fighter_id}
              fighter={fighter}
              isWinner={winnerId === fighter.fighter_id}
            />
          ))}
        </tr>
      </thead>
      <tbody>
        {METRICS.map((metric) => (
          <tr key={metric.label} className="border-t border-border">
            <th
              scope="row"
              className="py-3 pr-4 text-left align-top font-medium text-muted-foreground"
            >
              {metric.label}
              {metric.hint ? (
                <span className="block text-[0.65rem] uppercase tracking-widest text-muted-foreground/70">
                  {metric.hint}
                </span>
              ) : null}
            </th>
            {ordered.map((fighter) => (
              <td
                key={fighter.fighter_id}
                className="py-3 text-center align-top font-mono text-base tabular-nums"
              >
                {metric.value(fighter)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CornerHeader({
  fighter,
  isWinner,
}: {
  fighter: BoutFighterStatsOut;
  isWinner: boolean;
}) {
  const cornerAccent =
    fighter.corner === "red" ? "bg-corner-red" : "bg-corner-blue";

  return (
    <th scope="col" className="pb-4 align-bottom">
      <div className="relative flex flex-col items-center gap-1 px-2 pt-3">
        {/* Faixa do canto: vermelho ou azul, a linguagem do octógono. */}
        <span
          aria-hidden="true"
          className={`absolute inset-x-2 top-0 h-1 ${cornerAccent}`}
        />
        <span className="text-[0.6rem] font-medium uppercase tracking-[0.2em] text-muted-foreground">
          {fighter.corner === "red" ? "Canto vermelho" : "Canto azul"}
        </span>
        <Link
          to={`/fighters/${String(fighter.fighter_id)}`}
          aria-label={isWinner ? `${fighter.name}, vencedor` : undefined}
          className="text-center font-display text-lg font-semibold uppercase leading-tight tracking-wide hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
    </th>
  );
}
