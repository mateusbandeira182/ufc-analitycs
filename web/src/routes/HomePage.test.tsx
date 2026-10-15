import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { AppLayout } from "@/components/layout/AppLayout";
import { server } from "@/mocks/server";
import { HomePage } from "@/routes/HomePage";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderHome() {
  return renderWithProviders(<HomePage />, {
    routes: [
      { path: "/", element: <HomePage /> },
      { path: "/fighters", element: <div>Rota de lutadores</div> },
      { path: "/events/:id", element: <div>Rota do evento</div> },
    ],
    initialEntries: ["/"],
  });
}

/** Home dentro da casca, para exercitar a busca que agora vive no cabeçalho. */
function renderHomeInLayout() {
  return renderWithProviders(<HomePage />, {
    routes: [
      {
        element: <AppLayout />,
        children: [
          { index: true, element: <HomePage /> },
          { path: "/fighters", element: <div>Rota de lutadores</div> },
        ],
      },
    ],
    initialEntries: ["/"],
  });
}

describe("HomePage — card do último evento", () => {
  it("mostra o último evento realizado com nome e data", async () => {
    renderHome();

    expect(
      await screen.findByRole("heading", { level: 2, name: /ufc 300/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("13/04/2024")).toBeInTheDocument();
  });

  it("destaca a luta principal com os dois cantos e seus retratos", async () => {
    renderHome();

    const principal = await screen.findByRole("region", {
      name: /luta principal/i,
    });

    // A principal é `bouts[0]`: o servidor é a autoridade da ordem
    // (`bout_order asc nulls_last, id asc`) e a Home não calcula `Math.min`.
    expect(
      within(principal).getByRole("link", { name: /alex pereira, vencedor/i }),
    ).toHaveAttribute("href", "/fighters/100");
    expect(
      within(principal).getByRole("img", { name: /alex pereira/i }),
    ).toHaveAttribute("src", "https://example.test/pereira.png");
  });

  it("cai no fallback por iniciais no canto sem retrato", async () => {
    renderHome();

    const principal = await screen.findByRole("region", {
      name: /luta principal/i,
    });

    expect(within(principal).getByText("JH")).toBeInTheDocument();
    expect(
      within(principal).queryByRole("img", { name: /jamahal hill/i }),
    ).not.toBeInTheDocument();
  });

  it("mostra a contagem por método com o denominador e os destaques rotulados", async () => {
    renderHome();

    expect(await screen.findByText(/3 lutas realizadas/i)).toBeInTheDocument();
    expect(screen.getByText(/calculado, não oficial/i)).toBeInTheDocument();
    expect(screen.getByText(/2 de 3 lutas previstas/i)).toBeInTheDocument();
  });

  it("linka o card para a página do evento", async () => {
    const user = userEvent.setup();
    const { router } = renderHome();

    await user.click(
      await screen.findByRole("link", { name: /ver o card completo/i }),
    );

    expect(router.state.location.pathname).toBe("/events/42");
  });

  it("mostra o indicador de carregamento enquanto o último evento chega", () => {
    server.use(
      http.get("*/api/v1/events/latest", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    renderHome();

    expect(
      screen.getByLabelText(/carregando último evento/i),
    ).toBeInTheDocument();
  });

  it("declara o estado vazio quando o acervo não tem evento realizado (404)", async () => {
    server.use(
      http.get("*/api/v1/events/latest", () =>
        HttpResponse.json(
          { detail: "Nenhum event realizado" },
          { status: 404 },
        ),
      ),
    );

    renderHome();

    // Nunca tela quebrada nem bloco vazio e mudo.
    expect(
      await screen.findByText(/nenhum evento realizado no acervo/i),
    ).toBeInTheDocument();
  });

  it("mostra mensagem legível quando a API falha", async () => {
    server.use(
      http.get("*/api/v1/events/latest", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderHome();

    expect(
      await screen.findByText(/não foi possível carregar o último evento/i),
    ).toBeInTheDocument();
  });
});

describe("HomePage — tabela de próximos", () => {
  it("lista os próximos eventos com a linha inteira clicável", async () => {
    renderHome();

    const proximos = await screen.findByRole("region", { name: /próximos/i });
    const linha = await within(proximos).findByRole("listitem", {
      name: /ufc 335/i,
    });

    expect(within(linha).getByRole("link")).toHaveAttribute(
      "href",
      "/events/813",
    );
  });

  it("marca a linha de próximo cuja data já passou", async () => {
    renderHome();

    const proximos = await screen.findByRole("region", { name: /próximos/i });
    const linha = await within(proximos).findByRole("listitem", {
      name: /ufc 332/i,
    });

    expect(
      within(linha).getByText(/aguardando sincronização/i),
    ).toBeInTheDocument();
  });

  it("avisa de catálogo dessincronizado quando não há evento por vir", async () => {
    server.use(
      http.get("*/api/v1/events", () =>
        HttpResponse.json({ items: [], total: 0, limit: 5, offset: 0 }),
      ),
    );

    renderHome();

    expect(
      await screen.findByText(/catálogo dessincronizado/i),
    ).toBeInTheDocument();
  });
});

describe("HomePage — busca e guardas", () => {
  it("não tem mais a busca no hero da Home", async () => {
    renderHome();

    await screen.findByRole("heading", { level: 2, name: /ufc 300/i });

    // A busca saiu do hero e virou permanente no cabeçalho: na Home isolada
    // não existe campo de busca nenhum.
    expect(screen.queryByRole("search")).not.toBeInTheDocument();
  });

  it("busca pelo cabeçalho e navega para /fighters propagando `q`", async () => {
    const user = userEvent.setup();
    const { router } = renderHomeInLayout();

    const banner = screen.getByRole("banner");
    await user.type(
      within(banner).getByLabelText(/buscar lutador/i),
      "adesanya",
    );
    await user.keyboard("{Enter}");

    expect(await screen.findByText("Rota de lutadores")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/fighters");
    expect(router.state.location.search).toContain("q=adesanya");
    expect(router.state.location.search).not.toContain("name=");
  });

  it("não renderiza aviso de defasagem de features (D-09)", async () => {
    renderHome();

    await screen.findByRole("heading", { level: 2, name: /ufc 300/i });

    // Nenhum endpoint de evento lê o cache as-of, logo a Home não pode exibir
    // diagnóstico de defasagem.
    expect(screen.queryByText(/defasad/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/atributos recalculados/i),
    ).not.toBeInTheDocument();
  });
});
