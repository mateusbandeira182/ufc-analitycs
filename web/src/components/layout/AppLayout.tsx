import { useState } from "react";
import { NavLink, Outlet, Link, useNavigate } from "react-router";

import { SearchInput } from "@/features/fighters/SearchInput";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/fighters", label: "Lutadores" },
  { to: "/events", label: "Eventos" },
  { to: "/head-to-head", label: "Head-to-head" },
  { to: "/matchup", label: "Prevê essa luta" },
];

/**
 * Casca da aplicação: cabeçalho com navegação e **busca permanente** + conteúdo
 * da rota + rodapé.
 *
 * A busca deixou de ser um momento do hero da Home e virou parte da casca: num
 * acervo de 2.754 lutadores, procurar por nome é a ação mais frequente, e ela
 * não pode exigir voltar à Home. Submeter leva a `/fighters?q=`, o parâmetro que
 * casa nome **ou** apelido.
 */
export function AppLayout() {
  const navigate = useNavigate();
  const [term, setTerm] = useState("");

  function goToFighters(value: string) {
    const query = value.trim();
    void navigate(
      query ? `/fighters?q=${encodeURIComponent(query)}` : "/fighters",
    );
  }

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-10 border-b border-border bg-background/80 backdrop-blur">
        <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
          <Link
            to="/"
            className="flex items-center gap-2 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span
              aria-hidden="true"
              className="octagon-clip grid size-7 place-items-center bg-primary"
            >
              <span className="octagon-clip size-5 bg-background" />
            </span>
            <span className="font-display text-lg font-bold uppercase tracking-widest">
              MMA<span className="text-primary">·</span>Analytics
            </span>
          </Link>

          <div className="order-last w-full sm:order-none sm:ml-auto sm:w-56">
            <SearchInput
              id="header-search"
              label="Buscar lutador"
              hideLabel
              value={term}
              onChange={setTerm}
              onSubmit={goToFighters}
              placeholder="Buscar lutador..."
              inputClassName="h-9 text-sm"
            />
          </div>

          <nav aria-label="Principal">
            <ul className="flex flex-wrap items-center gap-1">
              {NAV_ITEMS.map(({ to, label }) => (
                <li key={to}>
                  <NavLink
                    to={to}
                    className={({ isActive }) =>
                      cn(
                        "rounded-md px-3 py-2 text-sm font-medium uppercase tracking-wide transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        isActive ? "text-primary" : "text-muted-foreground",
                      )
                    }
                  >
                    {label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </header>

      <main className="flex-1">
        <Outlet />
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto w-full max-w-5xl px-4 py-6 text-xs text-muted-foreground">
          Acervo read-only do UFC — dados históricos do Kaggle e incrementais da
          Cito.
        </div>
      </footer>
    </div>
  );
}
