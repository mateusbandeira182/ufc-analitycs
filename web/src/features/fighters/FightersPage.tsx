import { useSearchParams } from "react-router";

import { FighterList } from "@/features/fighters/FighterList";
import { SearchInput } from "@/features/fighters/SearchInput";
import { useFighters } from "@/features/fighters/useFighters";

/**
 * Rota `/fighters`: a URL (`?name=`) é a fonte da verdade do termo de busca —
 * torna a busca compartilhável. Orquestra o hook e delega os estados à lista.
 */
export function FightersPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const name = searchParams.get("name") ?? "";
  const query = useFighters(name);

  function handleSearchChange(value: string) {
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        if (value) {
          next.set("name", value);
        } else {
          next.delete("name");
        }
        return next;
      },
      { replace: true },
    );
  }

  return (
    <section className="mx-auto w-full max-w-5xl px-4 py-10">
      <header className="mb-6">
        <p className="font-display text-xs font-medium uppercase tracking-[0.3em] text-primary">
          Arquivo do octógono
        </p>
        <h1 className="font-display text-4xl font-bold uppercase tracking-tight">
          Lutadores
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Busque pelo nome e explore o cartel de cada atleta do acervo.
        </p>
      </header>

      <div className="mb-6 max-w-md">
        <SearchInput
          id="fighters-search"
          label="Buscar lutador"
          value={name}
          onChange={handleSearchChange}
          placeholder="Digite o nome do lutador..."
        />
      </div>

      {query.isSuccess ? (
        <p className="mb-4 font-mono text-xs uppercase tracking-widest text-muted-foreground">
          {query.data.total} {query.data.total === 1 ? "lutador" : "lutadores"}
        </p>
      ) : null}

      <FighterList
        fighters={query.data?.items ?? []}
        isPending={query.isPending}
        isError={query.isError}
      />
    </section>
  );
}
