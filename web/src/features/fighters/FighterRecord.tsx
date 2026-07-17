import type { FighterOut } from "@/api/schema";
import { Card } from "@/components/ui/card";
import {
  formatHeight,
  formatIsoDate,
  formatReach,
  formatRecord,
  formatStance,
  formatWeight,
} from "@/features/fighters/format";

interface FighterRecordProps {
  fighter: FighterOut;
}

/**
 * Cabeçalho do lutador: o cartel (V-D-E) em destaque e os atributos lentos
 * (altura, alcance, base, nascimento) formatados, com traço quando ausentes.
 * Componente de apresentação puro — os dados vêm da rota.
 */
export function FighterRecord({ fighter }: FighterRecordProps) {
  const attributes = [
    { label: "Altura", value: formatHeight(fighter.height_cm) },
    { label: "Alcance", value: formatReach(fighter.reach_cm) },
    { label: "Peso", value: formatWeight(fighter.weight_kg) },
    { label: "Base", value: formatStance(fighter.stance) },
    { label: "Nascimento", value: formatIsoDate(fighter.date_of_birth) },
  ];

  return (
    <Card className="relative overflow-hidden">
      {/* Faixa de acento — a mesma linguagem visual dos cards da lista. */}
      <span
        aria-hidden="true"
        className="absolute inset-y-0 left-0 w-1 bg-primary"
      />
      <div className="flex flex-col gap-6 p-6 pl-8 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="font-display text-xs font-medium uppercase tracking-[0.3em] text-primary">
            Cartel
          </p>
          <span className="mt-1 block font-mono text-4xl font-bold tabular-nums text-belt-gold">
            {formatRecord(fighter)}
          </span>
          <span className="text-[0.7rem] uppercase tracking-widest text-muted-foreground">
            Vitórias · Derrotas · Empates
          </span>
        </div>

        <dl className="grid grid-cols-2 gap-x-8 gap-y-3 sm:grid-cols-4">
          {attributes.map((attribute) => (
            <div key={attribute.label}>
              <dt className="text-[0.65rem] uppercase tracking-widest text-muted-foreground">
                {attribute.label}
              </dt>
              <dd className="mt-0.5 font-display text-lg font-semibold tabular-nums">
                {attribute.value}
              </dd>
            </div>
          ))}
        </dl>
      </div>
    </Card>
  );
}
