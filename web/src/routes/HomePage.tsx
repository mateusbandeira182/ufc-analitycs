import { ArrowRight, CalendarDays, Swords, Users } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { SearchInput } from "@/features/fighters/SearchInput";

const NAV_CARDS = [
  {
    to: "/fighters",
    label: "Lutadores",
    description: "Cartel, atributos e histórico luta a luta de cada atleta.",
    icon: Users,
  },
  {
    to: "/events",
    label: "Eventos",
    description: "Cards completos por evento, com data, local e resultados.",
    icon: CalendarDays,
  },
  {
    to: "/head-to-head",
    label: "Head-to-head",
    description: "Compare dois atletas lado a lado e veja o confronto direto.",
    icon: Swords,
  },
] as const;

/**
 * Home hub (`/`): a porta de entrada. Hero com a busca em destaque e os três
 * caminhos de exploração. Submeter a busca leva à lista propagando `?name=`.
 */
export function HomePage() {
  const navigate = useNavigate();
  const [term, setTerm] = useState("");

  function goToFighters(value: string) {
    const query = value.trim();
    void navigate(
      query ? `/fighters?name=${encodeURIComponent(query)}` : "/fighters",
    );
  }

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-12 sm:py-16">
      <section className="grid items-center gap-10 md:grid-cols-[1fr_auto]">
        <div>
          <p className="font-display text-xs font-semibold uppercase tracking-[0.35em] text-primary">
            Arquivo histórico granular do UFC
          </p>
          <h1 className="mt-3 font-display text-5xl font-bold uppercase leading-[0.95] tracking-tight sm:text-6xl">
            O arquivo do <span className="text-primary">octógono</span>
          </h1>
          <p className="mt-4 max-w-md text-base text-muted-foreground">
            Trinta anos de UFC guardados luta a luta. Comece pelo nome de um
            atleta e navegue pelo acervo.
          </p>

          <div className="mt-8 max-w-md">
            <SearchInput
              id="home-search"
              label="Buscar lutador"
              value={term}
              onChange={setTerm}
              onSubmit={goToFighters}
              placeholder="Ex.: Jon Jones, Adesanya..."
              inputClassName="h-12 text-base"
            />
            <Link
              to="/fighters"
              className="mt-3 inline-flex items-center gap-1 text-sm font-medium text-muted-foreground underline-offset-4 transition-colors hover:text-foreground"
            >
              Ver acervo completo
              <ArrowRight className="size-4" />
            </Link>
          </div>
        </div>

        {/* Assinatura: o octógono como emblema. */}
        <div
          aria-hidden="true"
          className="mx-auto hidden size-56 place-items-center md:grid"
        >
          <div className="octagon-clip grid size-56 place-items-center bg-gradient-to-br from-corner-red via-primary to-corner-blue p-[3px]">
            <div className="octagon-clip grid size-full place-items-center bg-card">
              <Swords className="size-16 text-belt-gold" />
            </div>
          </div>
        </div>
      </section>

      <nav aria-label="Explorar" className="mt-16">
        <ul className="grid gap-4 sm:grid-cols-3">
          {NAV_CARDS.map(({ to, label, description, icon: Icon }) => (
            <li key={to}>
              <Link
                to={to}
                aria-label={label}
                className="group block h-full rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                <Card className="h-full transition-colors group-hover:border-primary/60">
                  <CardHeader>
                    <span className="mb-2 grid size-10 place-items-center rounded-md bg-secondary text-primary">
                      <Icon className="size-5" />
                    </span>
                    <CardTitle className="flex items-center justify-between">
                      {label}
                      <ArrowRight className="size-4 text-muted-foreground transition-transform group-hover:translate-x-1 group-hover:text-primary" />
                    </CardTitle>
                    <CardDescription>{description}</CardDescription>
                  </CardHeader>
                </Card>
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}
