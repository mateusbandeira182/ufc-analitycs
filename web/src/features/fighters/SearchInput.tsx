import { Search } from "lucide-react";
import { type FormEvent } from "react";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

interface SearchInputProps {
  /** Id único do input, para associar o `label`. */
  id: string;
  /** Texto do rótulo (obrigatório para acessibilidade). */
  label: string;
  value: string;
  /** Chamado a cada tecla, com o novo valor. */
  onChange: (value: string) => void;
  /** Chamado no submit do formulário (Enter), com o valor atual. */
  onSubmit?: (value: string) => void;
  placeholder?: string;
  /** Esconde o rótulo visualmente, mantendo-o para leitores de tela. */
  hideLabel?: boolean;
  className?: string;
  inputClassName?: string;
}

/**
 * Campo de busca acessível: `label` associado ao input, ícone decorativo e foco
 * visível herdado do Input. Reutilizado pela lista e pela home hub.
 */
export function SearchInput({
  id,
  label,
  value,
  onChange,
  onSubmit,
  placeholder,
  hideLabel = false,
  className,
  inputClassName,
}: SearchInputProps) {
  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit?.(value);
  }

  return (
    <form
      role="search"
      onSubmit={handleSubmit}
      className={cn("w-full", className)}
    >
      <label
        htmlFor={id}
        className={cn(
          "mb-2 block font-display text-xs font-medium uppercase tracking-widest text-muted-foreground",
          hideLabel && "sr-only",
        )}
      >
        {label}
      </label>
      <div className="relative">
        <Search
          aria-hidden="true"
          className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
        />
        <Input
          id={id}
          type="search"
          value={value}
          onChange={(event) => {
            onChange(event.target.value);
          }}
          placeholder={placeholder}
          autoComplete="off"
          className={cn("pl-9", inputClassName)}
        />
      </div>
    </form>
  );
}
