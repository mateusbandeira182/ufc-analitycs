import { Input } from "@/components/ui/input";
import {
  LAST_N_OPTIONS,
  type StatsFilter,
} from "@/features/fighters/statsFilter";
import { cn } from "@/lib/utils";

interface FighterStatsFiltersProps {
  filter: StatsFilter;
  onChange: (next: StatsFilter) => void;
}

/**
 * Controles do recorte das estatísticas: um segmentado "Últimas N lutas" (2–8 +
 * Todas) e um intervalo de datas (De/Até). Componente controlado — não guarda
 * estado; o pai detém o `StatsFilter` e reage a cada mudança. Os dois eixos
 * combinam por interseção (ver `statsFilter`).
 */
export function FighterStatsFilters({
  filter,
  onChange,
}: FighterStatsFiltersProps) {
  return (
    <div className="flex flex-col gap-6 rounded-lg border border-border bg-card/50 p-5 sm:flex-row sm:flex-wrap sm:items-end sm:justify-between">
      <fieldset>
        <legend className="mb-2 text-[0.65rem] font-medium uppercase tracking-widest text-muted-foreground">
          Últimas lutas
        </legend>
        <div
          role="group"
          aria-label="Recortar pelas últimas lutas"
          className="flex flex-wrap gap-1"
        >
          {LAST_N_OPTIONS.map((n) => (
            <SegmentButton
              key={n}
              active={filter.lastN === n}
              onClick={() => {
                onChange({ ...filter, lastN: n });
              }}
            >
              {n}
            </SegmentButton>
          ))}
          <SegmentButton
            active={filter.lastN === null}
            onClick={() => {
              onChange({ ...filter, lastN: null });
            }}
          >
            Todas
          </SegmentButton>
        </div>
      </fieldset>

      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          {/* Rótulo visual; o nome acessível do campo vem do `aria-label`. */}
          <span
            aria-hidden="true"
            className="text-[0.65rem] font-medium uppercase tracking-widest text-muted-foreground"
          >
            De
          </span>
          <Input
            type="date"
            aria-label="Período: data inicial"
            value={filter.dateFrom ?? ""}
            max={filter.dateTo ?? undefined}
            onChange={(event) => {
              onChange({ ...filter, dateFrom: event.target.value || null });
            }}
            className="w-40"
          />
        </div>
        <div className="flex flex-col gap-1">
          <span
            aria-hidden="true"
            className="text-[0.65rem] font-medium uppercase tracking-widest text-muted-foreground"
          >
            Até
          </span>
          <Input
            type="date"
            aria-label="Período: data final"
            value={filter.dateTo ?? ""}
            min={filter.dateFrom ?? undefined}
            onChange={(event) => {
              onChange({ ...filter, dateTo: event.target.value || null });
            }}
            className="w-40"
          />
        </div>
      </div>
    </div>
  );
}

/**
 * Botão de um segmento. O estado ativo é sinalizado por `aria-pressed` (não só
 * pela cor), para leitores de tela e para os testes.
 */
function SegmentButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "min-w-9 rounded-md border px-3 py-1.5 font-mono text-sm font-semibold tabular-nums transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        active
          ? "border-belt-gold bg-belt-gold/15 text-belt-gold"
          : "border-border text-muted-foreground hover:border-belt-gold/50 hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}
