import { Link, useSearchParams } from "react-router";

import { FighterList } from "@/features/fighters/FighterList";
import { SearchInput } from "@/features/fighters/SearchInput";
import { useFighters } from "@/features/fighters/useFighters";
import { Pagination } from "@/components/ui/pagination";
import { getPageInfo, parsePaginationParams } from "@/lib/pagination";

/**
 * Rota `/fighters`: a URL é a fonte da verdade — `?name=` (termo de busca) e
 * `?limit=`/`?offset=` (paginação) tornam a listagem compartilhável. Orquestra o
 * hook e delega os estados à lista; mudar a busca reseta para a primeira página.
 */
export function FightersPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const name = searchParams.get("name") ?? "";
  const { limit, offset } = parsePaginationParams(searchParams);
  const query = useFighters({ name, limit, offset });

  function handleSearchChange(value: string) {
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        if (value) {
          next.set("name", value);
        } else {
          next.delete("name");
        }
        // Um novo termo recomeça a paginação — o offset antigo não faz sentido.
        next.delete("offset");
        return next;
      },
      { replace: true },
    );
  }

  const data = query.data;
  const pageInfo = data ? getPageInfo(data) : null;

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

      {pageInfo?.isOutOfRange ? (
        <OutOfRangeNotice />
      ) : (
        <>
          <FighterList
            fighters={data?.items ?? []}
            isPending={query.isPending}
            isError={query.isError}
          />

          {data && pageInfo && pageInfo.totalPages > 1 ? (
            <Pagination
              total={data.total}
              limit={data.limit}
              offset={data.offset}
              label="lutadores"
            />
          ) : null}
        </>
      )}
    </section>
  );
}

/**
 * Página fora do intervalo: o `offset` da URL aponta além do acervo (link
 * compartilhado desatualizado, por exemplo). Orienta o usuário de volta à
 * primeira página preservando o termo de busca.
 */
function OutOfRangeNotice() {
  const [searchParams] = useSearchParams();
  const params = new URLSearchParams(searchParams);
  params.delete("offset");
  const query = params.toString();
  const firstPageHref = query ? `/fighters?${query}` : "/fighters";

  return (
    <div
      role="status"
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/50 px-6 py-16 text-center"
    >
      <p className="font-display text-lg font-semibold uppercase tracking-wide">
        Página fora do intervalo
      </p>
      <p className="max-w-sm text-sm text-muted-foreground">
        Não há lutadores nesta página do acervo.
      </p>
      <Link
        to={firstPageHref}
        className="mt-2 font-display text-xs font-semibold uppercase tracking-widest text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        Voltar à primeira página
      </Link>
    </div>
  );
}
