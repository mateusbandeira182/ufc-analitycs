import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { EventsPage } from "@/features/events/EventsPage";
import { server } from "@/mocks/server";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderEventsPage(initialEntry = "/events") {
  return renderWithProviders(<EventsPage />, {
    routes: [{ path: "/events", element: <EventsPage /> }],
    initialEntries: [initialEntry],
  });
}

/** A seção pelo seu cabeçalho acessível, para escopar as asserções. */
function section(name: RegExp): HTMLElement {
  return screen.getByRole("region", { name });
}

describe("EventsPage — as duas seções", () => {
  it("renderiza Realizados e Próximos com listas DIFERENTES na mesma tela", async () => {
    // Prova de que `when` entra na query key: sem ele as duas seções leriam a
    // mesma entrada de cache e exibiriam a mesma lista, com paginação
    // independente na URL e conteúdo idêntico na tela.
    renderEventsPage();

    const realizados = await screen.findByRole("region", {
      name: /realizados/i,
    });
    const proximos = screen.getByRole("region", { name: /próximos/i });

    const nomesRealizados = (
      await within(realizados).findAllByRole("listitem")
    ).map((li) => li.textContent);
    const nomesProximos = (
      await within(proximos).findAllByRole("listitem")
    ).map((li) => li.textContent);

    expect(nomesRealizados.length).toBeGreaterThan(0);
    expect(nomesProximos.length).toBeGreaterThan(0);
    expect(nomesRealizados).not.toEqual(nomesProximos);
    // Nenhum evento aparece nas duas seções: a partição é disjunta.
    for (const nome of nomesProximos) {
      expect(nomesRealizados).not.toContain(nome);
    }
  });

  it("mostra o evento realizado em Realizados e o por vir em Próximos", async () => {
    renderEventsPage();

    expect(
      await within(section(/realizados/i)).findByText("UFC 300"),
    ).toBeInTheDocument();
    expect(
      await within(section(/próximos/i)).findByText("UFC 335"),
    ).toBeInTheDocument();
    expect(
      within(section(/realizados/i)).queryByText("UFC 335"),
    ).not.toBeInTheDocument();
  });

  it("declara o total de cada seção separadamente", async () => {
    renderEventsPage();

    expect(
      await within(section(/realizados/i)).findByText(/25 eventos/i),
    ).toBeInTheDocument();
    expect(
      await within(section(/próximos/i)).findByText(/3 eventos/i),
    ).toBeInTheDocument();
  });
});

describe("EventsPage — paginação independente", () => {
  it("lê a posição de cada seção da URL (`?realizados=3&proximos=1`)", async () => {
    renderEventsPage("/events?realizados=3&proximos=1");

    const realizados = section(/realizados/i);
    // Página 3 de "Realizados" (10 por página, 25 eventos) não contém o UFC 300.
    expect(await within(realizados).findByText("UFC 281")).toBeInTheDocument();
    expect(within(realizados).queryByText("UFC 300")).not.toBeInTheDocument();
    expect(
      within(realizados).getByRole("navigation", {
        name: /paginação de eventos realizados/i,
      }),
    ).toHaveTextContent(/página 3 de 3/i);

    expect(
      await within(section(/próximos/i)).findByText("UFC 335"),
    ).toBeInTheDocument();
  });

  it("paginar Realizados reescreve só `realizados`, deixando `proximos` intacto", async () => {
    const user = userEvent.setup();
    const { router } = renderEventsPage("/events?realizados=2&proximos=1");

    await within(section(/realizados/i)).findByRole("navigation", {
      name: /paginação de eventos realizados/i,
    });
    await user.click(
      within(section(/realizados/i)).getByRole("link", { name: /próxima/i }),
    );

    const query = new URLSearchParams(router.state.location.search);
    expect(query.get("realizados")).toBe("3");
    expect(query.get("proximos")).toBe("1");
  });

  it("recarregar a URL preserva as duas posições", async () => {
    const { router } = renderEventsPage("/events?realizados=3&proximos=1");

    await within(section(/realizados/i)).findByText("UFC 281");

    const query = new URLSearchParams(router.state.location.search);
    expect(query.get("realizados")).toBe("3");
    expect(query.get("proximos")).toBe("1");
  });

  it("mostra o aviso de página fora do intervalo só na seção afetada", async () => {
    renderEventsPage("/events?realizados=9&proximos=1");

    const realizados = section(/realizados/i);
    expect(
      await within(realizados).findByText(/página fora do intervalo/i),
    ).toBeInTheDocument();

    // A outra seção segue mostrando sua lista — o problema é de uma só.
    expect(
      await within(section(/próximos/i)).findByText("UFC 335"),
    ).toBeInTheDocument();
    expect(
      within(section(/próximos/i)).queryByText(/fora do intervalo/i),
    ).not.toBeInTheDocument();
  });

  it("o link de volta à primeira página zera só a seção afetada", async () => {
    const user = userEvent.setup();
    const { router } = renderEventsPage("/events?realizados=9&proximos=1");

    await user.click(
      await within(section(/realizados/i)).findByRole("link", {
        name: /primeira página/i,
      }),
    );

    const query = new URLSearchParams(router.state.location.search);
    expect(query.has("realizados")).toBe(false);
    expect(query.get("proximos")).toBe("1");
    expect(
      await within(section(/realizados/i)).findByText("UFC 300"),
    ).toBeInTheDocument();
  });
});

describe("EventsPage — marcador de sincronização", () => {
  it("marca a linha de Próximos cuja data já passou", async () => {
    renderEventsPage();

    const proximos = section(/próximos/i);
    const linha = await within(proximos).findByRole("listitem", {
      name: /ufc 332/i,
    });

    expect(
      within(linha).getByText(/aguardando sincronização/i),
    ).toBeInTheDocument();
  });

  it("não marca a linha de Próximos cuja data ainda não chegou", async () => {
    renderEventsPage();

    const proximos = section(/próximos/i);
    const linha = await within(proximos).findByRole("listitem", {
      name: /ufc 335/i,
    });

    expect(
      within(linha).queryByText(/aguardando sincronização/i),
    ).not.toBeInTheDocument();
  });

  it("não marca nenhuma linha de Realizados", async () => {
    renderEventsPage();

    const realizados = section(/realizados/i);
    await within(realizados).findByText("UFC 300");

    expect(
      within(realizados).queryByText(/aguardando sincronização/i),
    ).not.toBeInTheDocument();
  });
});

describe("EventsPage — estados e guardas", () => {
  it("mostra o skeleton de carregamento em cada seção enquanto os dados chegam", () => {
    server.use(
      http.get("*/api/v1/events", async () => {
        await delay("infinite");
        return HttpResponse.json({ items: [], total: 0, limit: 10, offset: 0 });
      }),
    );

    renderEventsPage();

    expect(screen.getAllByTestId("events-loading")).toHaveLength(2);
  });

  it("mostra mensagem legível de erro quando o backend falha", async () => {
    server.use(
      http.get("*/api/v1/events", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderEventsPage();

    // As duas seções falham de forma independente: cada uma tem sua própria
    // entrada de cache, logo cada uma mostra a própria mensagem.
    await waitFor(() => {
      expect(
        screen.getAllByText(/não foi possível carregar os eventos/i),
      ).toHaveLength(2);
    });
  });

  it("mostra estado vazio por variante quando a seção não tem evento", async () => {
    server.use(
      http.get("*/api/v1/events", ({ request }) => {
        const when = new URL(request.url).searchParams.get("when");
        if (when === "upcoming") {
          return HttpResponse.json({
            items: [],
            total: 0,
            limit: 10,
            offset: 0,
          });
        }
        return HttpResponse.json({
          items: [
            {
              id: 1,
              name: "UFC 1",
              date: "1993-11-12",
              location: null,
              source: "kaggle",
            },
          ],
          total: 1,
          limit: 10,
          offset: 0,
        });
      }),
    );

    renderEventsPage();

    // O estado vazio de "Próximos" fala do catálogo dessincronizado, não de
    // "nenhum evento no acervo" — o acervo tem; é o card futuro que falta.
    expect(
      await within(section(/próximos/i)).findByText(
        /catálogo dessincronizado/i,
      ),
    ).toBeInTheDocument();
    expect(
      await within(section(/realizados/i)).findByText("UFC 1"),
    ).toBeInTheDocument();
  });

  it("não renderiza aviso de defasagem de features (D-09)", async () => {
    renderEventsPage();

    await within(section(/realizados/i)).findByText("UFC 300");

    // Nenhum endpoint de /events lê o cache as-of, logo a tela não pode exibir
    // diagnóstico de defasagem — seria dado que o endpoint não serve.
    expect(screen.queryByText(/defasad/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/atributos recalculados/i),
    ).not.toBeInTheDocument();
  });
});
