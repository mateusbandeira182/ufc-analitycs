import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { EventPage } from "@/features/events/EventPage";
import { EventsPage } from "@/features/events/EventsPage";
import { EVENT_DETAIL_FIXTURES } from "@/mocks/events";
import { server } from "@/mocks/server";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderEventPage(id: number | string) {
  return renderWithProviders(<EventPage />, {
    routes: [{ path: "/events/:id", element: <EventPage /> }],
    initialEntries: [`/events/${String(id)}`],
  });
}

/** Responde o detalhe desta fixture em qualquer id pedido. */
function serveDetail(id: number) {
  server.use(
    http.get("*/api/v1/events/:id", () =>
      HttpResponse.json(EVENT_DETAIL_FIXTURES[id]),
    ),
  );
}

describe("EventPage — cabeçalho e navegação", () => {
  it("mostra o skeleton enquanto o evento carrega", () => {
    server.use(
      http.get("*/api/v1/events/:id", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    renderEventPage(42);

    expect(screen.getByLabelText(/carregando evento/i)).toBeInTheDocument();
  });

  it("exibe o cabeçalho com nome, data formatada e local no sucesso", async () => {
    renderEventPage(42);

    expect(
      await screen.findByRole("heading", { level: 1, name: /ufc 300/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("13/04/2024")).toBeInTheDocument();
    expect(screen.getByText(/las vegas, usa/i)).toBeInTheDocument();
  });

  it("navega da lista para o detalhe do evento ao clicar", async () => {
    const user = userEvent.setup();
    const { router } = renderWithProviders(<EventsPage />, {
      routes: [
        { path: "/events", element: <EventsPage /> },
        { path: "/events/:id", element: <EventPage /> },
      ],
      initialEntries: ["/events"],
    });

    const link = await screen.findByRole("link", { name: /ufc 300/i });
    await user.click(link);

    expect(router.state.location.pathname).toBe("/events/42");
    expect(
      await screen.findByRole("heading", { level: 1, name: /ufc 300/i }),
    ).toBeInTheDocument();
  });
});

describe("EventPage — card agrupado por seção", () => {
  it("agrupa as lutas pelos rótulos de `card_section` do payload", async () => {
    renderEventPage(42);

    // Os rótulos vêm crus da Cito, nunca derivados de `bout_order` (a luta
    // principal traz 1001, que é o MENOR valor, e a faixa não é densa).
    const principal = await screen.findByRole("region", {
      name: /main card/i,
    });
    const preliminares = screen.getByRole("region", { name: /prelims/i });

    expect(
      within(principal).getByRole("listitem", {
        name: /alex pereira vs jamahal hill/i,
      }),
    ).toBeInTheDocument();
    expect(
      within(preliminares).getByRole("listitem", {
        name: /bo nickal vs cody brundage/i,
      }),
    ).toBeInTheDocument();
    // A luta das preliminares não vaza para o card principal.
    expect(
      within(principal).queryByRole("listitem", { name: /bo nickal/i }),
    ).not.toBeInTheDocument();
  });

  it("mantém a ordem do servidor dentro de cada seção", async () => {
    renderEventPage(42);

    const principal = await screen.findByRole("region", {
      name: /main card/i,
    });
    const lutas = within(principal).getAllByRole("listitem");

    expect(lutas[0]).toHaveTextContent(/alex pereira/i);
    expect(lutas[1]).toHaveTextContent(/charles oliveira/i);
  });

  it("agrupa sob um rótulo declarado quando `card_section` é nulo", async () => {
    serveDetail(801);

    renderEventPage(801);

    // Card antigo ou não enriquecido: o contexto da Cito só cobre evento
    // recente e de forma irregular. A seção sem rótulo não fica sem cabeçalho.
    expect(
      await screen.findByRole("region", { name: /lutas sem seção/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", {
        name: /belal muhammad vs shavkat rakhmonov/i,
      }),
    ).toBeInTheDocument();
  });
});

describe("EventPage — cantos, vencedor e retrato", () => {
  it("renderiza o card 'A vs B' com cada lutador linkando para /fighters/:id", async () => {
    renderEventPage(42);

    // Escopado à luta: o bloco de destaques também linka o lutador, e é certo
    // que linke — ambos levam ao mesmo lugar.
    const luta = await screen.findByRole("listitem", {
      name: /alex pereira vs jamahal hill/i,
    });

    expect(
      within(luta).getByRole("link", { name: /alex pereira/i }),
    ).toHaveAttribute("href", "/fighters/100");
    expect(
      within(luta).getByRole("link", { name: /jamahal hill/i }),
    ).toHaveAttribute("href", "/fighters/200");
  });

  it("destaca o vencedor cruzando winner_id com fighter_id", async () => {
    renderEventPage(42);

    const winner = await screen.findByRole("link", {
      name: /alex pereira, vencedor/i,
    });
    expect(winner).toHaveAttribute("href", "/fighters/100");

    expect(
      screen.getByRole("link", { name: /jamahal hill/i }),
    ).not.toHaveAccessibleName(/vencedor/i);
  });

  it("não destaca nenhum canto quando winner_id é nulo (no contest)", async () => {
    renderEventPage(42);

    const oliveira = await screen.findByRole("link", {
      name: /charles oliveira/i,
    });
    expect(oliveira).not.toHaveAccessibleName(/vencedor/i);
    expect(
      screen.getByRole("link", { name: /arman tsarukyan/i }),
    ).not.toHaveAccessibleName(/vencedor/i);
  });

  it("mostra o retrato quando há `headshot_url`", async () => {
    renderEventPage(42);

    const retrato = await screen.findByRole("img", { name: /alex pereira/i });
    expect(retrato).toHaveAttribute("src", "https://example.test/pereira.png");
  });

  it("cai no fallback por iniciais quando `headshot_url` é nulo", async () => {
    renderEventPage(42);

    const luta = await screen.findByRole("listitem", {
      name: /alex pereira vs jamahal hill/i,
    });

    // Só 794 dos 2.754 lutadores têm retrato, e lutador histórico nunca terá:
    // o fallback é o caso comum e não pode parecer um buraco na tela.
    expect(within(luta).getByText("JH")).toBeInTheDocument();
    expect(
      within(luta).queryByRole("img", { name: /jamahal hill/i }),
    ).not.toBeInTheDocument();
  });

  it("exibe o resultado formatado (método, round/tempo e categoria de peso)", async () => {
    renderEventPage(42);

    const bout = await screen.findByRole("listitem", {
      name: /alex pereira vs jamahal hill/i,
    });
    expect(within(bout).getByText("KO/TKO")).toBeInTheDocument();
    expect(within(bout).getByText("R2 3:45")).toBeInTheDocument();
    expect(within(bout).getByText(/light heavyweight/i)).toBeInTheDocument();
  });

  it("linka cada luta para /bouts/:id", async () => {
    renderEventPage(42);

    const bout = await screen.findByRole("listitem", {
      name: /alex pereira vs jamahal hill/i,
    });
    expect(
      within(bout).getByRole("link", { name: /ver luta/i }),
    ).toHaveAttribute("href", "/bouts/7");
  });
});

describe("EventPage — resumo, destaques e ausência", () => {
  it("mostra a contagem por método com o denominador e os destaques rotulados", async () => {
    renderEventPage(42);

    expect(await screen.findByText(/3 lutas realizadas/i)).toBeInTheDocument();
    expect(screen.getByText(/calculado, não oficial/i)).toBeInTheDocument();
    expect(screen.getByText(/2 de 3 lutas previstas/i)).toBeInTheDocument();
  });

  it("não mostra contagem nem destaque quando o card inteiro está agendado", async () => {
    serveDetail(801);

    renderEventPage(801);

    expect(
      await screen.findByRole("heading", { level: 1, name: /ufc 332/i }),
    ).toBeInTheDocument();
    // `summary` e `highlights` chegam nulos: ausência de resultado não é zero,
    // e "0 finalizações" seria uma afirmação sobre o evento.
    expect(screen.queryByText(/lutas realizadas/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/calculado, não oficial/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/como terminou/i)).not.toBeInTheDocument();
  });

  it("mostra 'card ainda não publicado' no evento sem nenhuma luta", async () => {
    serveDetail(813);

    renderEventPage(813);

    // Uma mensagem única, sem ramificar pela data: com zero luta não existe
    // `status` para consultar, e a frase é verdadeira no futuro não publicado e
    // no passado não ingerido.
    expect(
      await screen.findByText(/card ainda não publicado/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/lutas realizadas/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/nenhuma luta registrada/i),
    ).not.toBeInTheDocument();
  });

  it("não renderiza aviso de defasagem de features (D-09)", async () => {
    renderEventPage(42);

    await screen.findByRole("heading", { level: 1, name: /ufc 300/i });

    expect(screen.queryByText(/defasad/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/atributos recalculados/i),
    ).not.toBeInTheDocument();
  });
});

describe("EventPage — erros", () => {
  it("mostra 'evento não encontrado' quando o evento não existe (404)", async () => {
    renderEventPage(999);

    expect(
      await screen.findByText(/evento não encontrado/i),
    ).toBeInTheDocument();
  });

  it("mostra 'evento não encontrado' quando o id da rota é inválido", async () => {
    renderEventPage("abc");

    expect(
      await screen.findByText(/evento não encontrado/i),
    ).toBeInTheDocument();
  });

  it("mostra mensagem legível quando o backend falha", async () => {
    server.use(
      http.get("*/api/v1/events/:id", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderEventPage(42);

    expect(
      await screen.findByText(/não foi possível carregar o evento/i),
    ).toBeInTheDocument();
  });
});
