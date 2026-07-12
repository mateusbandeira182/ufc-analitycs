import { Check, Search } from "lucide-react";
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

import type { FighterOut } from "@/api/schema";
import { Input } from "@/components/ui/input";
import { useFighters } from "@/features/fighters/useFighters";
import { formatRecord } from "@/features/fighters/format";
import { cn } from "@/lib/utils";

interface FighterSelectorProps {
  /** Rótulo do campo, associado ao input (ex.: "Lutador A"). */
  label: string;
  /** Id do lutador já escolhido (marca a opção correspondente); nulo se nenhum. */
  selectedId: number | null;
  /** Chamado ao escolher uma opção do dropdown, com o id do lutador. */
  onSelect: (fighterId: number) => void;
}

/** Espera `delayMs` de silêncio antes de propagar o valor — evita buscar por tecla. */
function useDebouncedValue(value: string, delayMs: number): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(value);
    }, delayMs);
    return () => {
      clearTimeout(timer);
    };
  }, [value, delayMs]);
  return debounced;
}

/**
 * Campo de busca com autocomplete: um combobox acessível que reaproveita a busca
 * server-side de lutadores (`useFighters`, debounced). Digitar filtra as opções;
 * escolher uma emite o id. Operável por teclado (setas, Enter, Esc).
 */
export function FighterSelector({
  label,
  selectedId,
  onSelect,
}: FighterSelectorProps) {
  const inputId = useId();
  const listId = useId();
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const blurTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );

  const term = useDebouncedValue(text.trim(), 250);
  const query = useFighters(term);
  const options: FighterOut[] =
    term.length >= 1 ? (query.data?.items ?? []) : [];
  const showList = open && options.length > 0;

  function choose(fighter: FighterOut) {
    setText(fighter.name);
    setOpen(false);
    setActiveIndex(-1);
    onSelect(fighter.id);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setActiveIndex((index) => Math.min(index + 1, options.length - 1));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => Math.max(index - 1, 0));
      return;
    }
    if (event.key === "Enter" && showList) {
      const target = options[activeIndex];
      if (target) {
        event.preventDefault();
        choose(target);
      }
      return;
    }
    if (event.key === "Escape") {
      setOpen(false);
      setActiveIndex(-1);
    }
  }

  const activeOptionId =
    showList && activeIndex >= 0
      ? `${listId}-${String(activeIndex)}`
      : undefined;

  return (
    <div className="relative">
      <label
        htmlFor={inputId}
        className="mb-2 block font-display text-xs font-medium uppercase tracking-widest text-muted-foreground"
      >
        {label}
      </label>
      <div className="relative">
        <Search
          aria-hidden="true"
          className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
        />
        <Input
          id={inputId}
          type="text"
          role="combobox"
          aria-expanded={showList}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeOptionId}
          autoComplete="off"
          value={text}
          placeholder="Busque pelo nome do lutador..."
          className="pl-9"
          onChange={(event) => {
            setText(event.target.value);
            setOpen(true);
            setActiveIndex(-1);
          }}
          onFocus={() => {
            setOpen(true);
          }}
          onKeyDown={handleKeyDown}
          onBlur={() => {
            // Fecha após o clique numa opção ter chance de disparar.
            blurTimer.current = setTimeout(() => {
              setOpen(false);
            }, 120);
          }}
        />
      </div>

      {showList ? (
        <ul
          id={listId}
          role="listbox"
          aria-label={`Resultados para ${label}`}
          className="absolute z-10 mt-1 max-h-64 w-full overflow-auto rounded-lg border border-border bg-popover p-1 shadow-lg"
        >
          {options.map((fighter, index) => {
            const isActive = index === activeIndex;
            const isSelected = fighter.id === selectedId;
            return (
              // O teclado é tratado no input (setas/Enter) via aria-activedescendant;
              // a opção não é focável, então um key listener nela nunca dispararia.
              // eslint-disable-next-line jsx-a11y/click-events-have-key-events -- combobox aria-activedescendant
              <li
                key={fighter.id}
                id={`${listId}-${String(index)}`}
                role="option"
                aria-selected={isSelected}
                className={cn(
                  "flex cursor-pointer items-center justify-between gap-3 rounded-md px-3 py-2 text-sm",
                  isActive && "bg-secondary",
                )}
                onMouseEnter={() => {
                  setActiveIndex(index);
                }}
                onMouseDown={(event) => {
                  // Impede o blur do input antes do clique registrar a escolha.
                  event.preventDefault();
                }}
                onClick={() => {
                  choose(fighter);
                }}
              >
                <span className="min-w-0 truncate">
                  <span className="font-display font-semibold uppercase tracking-wide">
                    {fighter.name}
                  </span>
                  {fighter.nickname ? (
                    <span className="ml-2 truncate italic text-muted-foreground">
                      &ldquo;{fighter.nickname}&rdquo;
                    </span>
                  ) : null}
                </span>
                <span className="flex shrink-0 items-center gap-2">
                  <span className="font-mono text-xs tabular-nums text-belt-gold">
                    {formatRecord(fighter)}
                  </span>
                  {isSelected ? (
                    <Check aria-hidden="true" className="size-4 text-primary" />
                  ) : null}
                </span>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
