import { NavLink, Outlet, Link } from "react-router";

import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/fighters", label: "Lutadores" },
  { to: "/events", label: "Eventos" },
  { to: "/head-to-head", label: "Head-to-head" },
];

/** Casca da aplicação: cabeçalho de navegação + conteúdo da rota + rodapé. */
export function AppLayout() {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-10 border-b border-border bg-background/80 backdrop-blur">
        <div className="mx-auto flex w-full max-w-5xl items-center justify-between px-4 py-3">
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
          <nav aria-label="Principal">
            <ul className="flex items-center gap-1">
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
