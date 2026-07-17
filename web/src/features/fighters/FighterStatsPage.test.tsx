import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { BoutMethod, FighterBoutOut } from "@/api/schema";
import { FighterPage } from "@/features/fighters/FighterPage";
import { FighterStatsPage } from "@/features/fighters/FighterStatsPage";
import { server } from "@/mocks/server";
import { NULL_STRIKE_SPLITS } from "@/mocks/strikeSplits";
import { renderWithProviders } from "@/test/renderWithProviders";

/*
  A página de estatísticas recompõe as médias client-side a partir do histórico
  granular (`/fighters/:id/bouts`), sob o recorte de últimas N lutas e/ou período.
  Os testes montam históricos controlados e verificam as médias derivadas e a
  reação aos filtros.
*/

interface BoutOverrides {
  id: number;
  date: string;
  won?: boolean;
  method?: BoutMethod;
  sig?: number | null;
  td?: number | null;
  control?: number | null;
}

/** Uma luta do histórico com box-score controlado (os demais campos são inertes aqui). */
function makeBout(overrides: BoutOverrides): FighterBoutOut {
  return {
    bout_id: overrides.id,
    event_id: overrides.id,
    event_name: `Evento ${String(overrides.id)}`,
    event_date: overrides.date,
    method: overrides.method ?? "decision",
    round: 3,
    ending_time_seconds: 300,
    won: overrides.won ?? false,
    opponent: { fighter_id: 99, name: "Adversário" },
    stats: {
      fighter_id: 1,
      name: "Jon Jones",
      corner: "red",
      knockdowns: null,
      sig_strikes_landed: overrides.sig ?? null,
      sig_strikes_attempted: null,
      takedowns_landed: overrides.td ?? null,
      takedowns_attempted: null,
      submission_attempts: null,
      control_time_seconds: overrides.control ?? null,
      ...NULL_STRIKE_SPLITS,
      source: "kaggle",
    },
  };
}

// Histórico ascendente de 5 lutas, com stats crescentes, para exercitar os recortes.
const RICH_HISTORY: FighterBoutOut[] = [
  makeBout({ id: 1, date: "2016-01-01", sig: 10, td: 5, control: 100 }),
  makeBout({
    id: 2,
    date: "2018-01-01",
    sig: 20,
    td: 4,
    control: 200,
    won: true,
    method: "ko_tko",
  }),
  makeBout({
    id: 3,
    date: "2020-01-01",
    sig: 30,
    td: 3,
    control: 300,
    won: true,
    method: "submission",
  }),
  makeBout({
    id: 4,
    date: "2022-01-01",
    sig: 40,
    td: 2,
    control: 400,
    won: true,
    method: "decision",
  }),
  makeBout({
    id: 5,
    date: "2024-01-01",
    sig: 50,
    td: 1,
    control: 500,
    won: true,
    method: "ko_tko",
  }),
];

/** Substitui o histórico servido para o lutador nos testes que precisam de um recorte. */
function serveHistory(bouts: FighterBoutOut[]) {
  server.use(
    http.get("*/api/v1/fighters/:id/bouts", () => HttpResponse.json(bouts)),
  );
}

function renderStatsPage(id: number | string) {
  return renderWithProviders(<FighterStatsPage />, {
    routes: [
      { path: "/fighters/:id", element: <FighterPage /> },
      { path: "/fighters/:id/stats", element: <FighterStatsPage /> },
    ],
    initialEntries: [`/fighters/${String(id)}/stats`],
  });
}

describe("FighterStatsPage", () => {
  it("exibe o nome do lutador e as médias por luta no sucesso", async () => {
    // Fixture padrão (JON_JONES_BOUTS): médias 60,0 / 1,0 / 3:24 sobre 2 lutas.
    renderStatsPage(1);

    expect(
      await screen.findByRole("heading", { name: /jon jones/i }),
    ).toBeInTheDocument();

    expect(await screen.findByText("60,0")).toBeInTheDocument();
    expect(screen.getByText("1,0")).toBeInTheDocument();
    expect(screen.getByText("3:24")).toBeInTheDocument();
  });

  it("separa o jogo em pé do jogo no chão (striker vs grappler)", async () => {
    renderStatsPage(1);

    expect(await screen.findByText(/em pé/i)).toBeInTheDocument();
    expect(screen.getByText(/no chão/i)).toBeInTheDocument();
  });

  it("mostra o número de lutas contabilizadas na média", async () => {
    renderStatsPage(1);

    expect(
      await screen.findByText(/2 lutas contabilizadas/i),
    ).toBeInTheDocument();
  });

  it("detalha como venceu por método com a contagem de cada finalização", async () => {
    serveHistory(RICH_HISTORY);
    renderStatsPage(1);

    const breakdown = await screen.findByRole("list", { name: /como venceu/i });
    const items = within(breakdown).getAllByRole("listitem");

    // Vitórias no histórico: 2 KO/TKO, 1 finalização, 1 decisão.
    expect(items).toHaveLength(3);
    expect(breakdown).toHaveTextContent("KO/TKO");
    expect(breakdown).toHaveTextContent("Finalização");
    expect(breakdown).toHaveTextContent("Decisão");
  });

  it("recorta pelas últimas N lutas ao escolher no segmentado", async () => {
    const user = userEvent.setup();
    serveHistory(RICH_HISTORY);
    renderStatsPage(1);

    // Todas as 5 lutas: média de golpes (10+20+30+40+50)/5 = 30,0.
    expect(
      await screen.findByText(/5 lutas contabilizadas/i),
    ).toBeInTheDocument();
    expect(screen.getByText("30,0")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "3" }));

    // Últimas 3 (2020/2022/2024): (30+40+50)/3 = 40,0.
    expect(
      await screen.findByText(/3 lutas contabilizadas/i),
    ).toBeInTheDocument();
    expect(screen.getByText("40,0")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "3" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    // "Todas" desfaz o recorte e volta às 5 lutas.
    await user.click(screen.getByRole("button", { name: "Todas" }));
    expect(
      await screen.findByText(/5 lutas contabilizadas/i),
    ).toBeInTheDocument();
    expect(screen.getByText("30,0")).toBeInTheDocument();
  });

  it("recorta por período de datas (De/Até)", async () => {
    serveHistory(RICH_HISTORY);
    renderStatsPage(1);

    expect(
      await screen.findByText(/5 lutas contabilizadas/i),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/data inicial/i), {
      target: { value: "2018-01-01" },
    });
    fireEvent.change(screen.getByLabelText(/data final/i), {
      target: { value: "2020-12-31" },
    });

    // Período mantém 2018 e 2020: média de golpes (20+30)/2 = 25,0.
    expect(
      await screen.findByText(/2 lutas contabilizadas/i),
    ).toBeInTheDocument();
    expect(screen.getByText("25,0")).toBeInTheDocument();
  });

  it("combina período e últimas N por interseção", async () => {
    const user = userEvent.setup();
    serveHistory(RICH_HISTORY);
    renderStatsPage(1);

    await screen.findByText(/5 lutas contabilizadas/i);

    fireEvent.change(screen.getByLabelText(/data inicial/i), {
      target: { value: "2016-01-01" },
    });
    fireEvent.change(screen.getByLabelText(/data final/i), {
      target: { value: "2020-12-31" },
    });
    await user.click(screen.getByRole("button", { name: "2" }));

    // Período deixa {2016,2018,2020}; as últimas 2 são {2018,2020}: (20+30)/2 = 25,0.
    expect(
      await screen.findByText(/2 lutas contabilizadas/i),
    ).toBeInTheDocument();
    expect(screen.getByText("25,0")).toBeInTheDocument();
  });

  it("mantém os filtros e avisa quando o recorte não pega nenhuma luta", async () => {
    serveHistory(RICH_HISTORY);
    renderStatsPage(1);

    await screen.findByText(/5 lutas contabilizadas/i);

    fireEvent.change(screen.getByLabelText(/data inicial/i), {
      target: { value: "2030-01-01" },
    });

    expect(
      await screen.findByText(/nenhuma luta corresponde ao recorte/i),
    ).toBeInTheDocument();
    // Os filtros seguem visíveis para o usuário ajustar.
    expect(screen.getByRole("button", { name: "Todas" })).toBeInTheDocument();
  });

  it("substitui a barra por uma nota quando o lutador tem lutas mas nenhuma vitória", async () => {
    serveHistory([
      makeBout({ id: 1, date: "2020-01-01", sig: 30, won: false }),
      makeBout({ id: 2, date: "2021-01-01", sig: 40, won: false }),
    ]);

    renderStatsPage(1);

    expect(
      await screen.findByText(/nenhuma vitória registrada/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("list", { name: /como venceu/i }),
    ).not.toBeInTheDocument();
  });

  it("exibe o perfil de striking com os shares por alvo e por posição", async () => {
    renderStatsPage(1);

    // Aguarda o perfil carregar (o título aparece já no estado de carregamento).
    expect(await screen.findByText("55%")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /perfil de striking/i }),
    ).toBeInTheDocument();

    // Por alvo: cabeça 55%, corpo 20%, perna 25%.
    expect(screen.getByText("Cabeça")).toBeInTheDocument();
    expect(screen.getByText("Corpo")).toBeInTheDocument();
    expect(screen.getByText("Perna")).toBeInTheDocument();
    expect(screen.getByText("25%")).toBeInTheDocument();

    // Por posição: distância 70%, clinch 20%, solo 10%.
    expect(screen.getByText("Distância")).toBeInTheDocument();
    expect(screen.getByText("70%")).toBeInTheDocument();
    expect(screen.getByText("Clinch")).toBeInTheDocument();
    expect(screen.getByText("Solo")).toBeInTheDocument();
    expect(screen.getByText("10%")).toBeInTheDocument();
  });

  it("mostra 'sem dado' em cada share quando não há perfil de striking", async () => {
    // Adesanya (id 3): a fixture de stats traz o perfil todo nulo.
    renderStatsPage(3);

    // Seis shares (alvo + posição), todos sem dado — nunca NaN/Infinity na tela.
    expect(await screen.findAllByText(/sem dado/i)).toHaveLength(6);
    expect(
      screen.getByRole("heading", { name: /perfil de striking/i }),
    ).toBeInTheDocument();
  });

  it("mostra mensagem legível quando o perfil de striking falha", async () => {
    server.use(
      http.get("*/api/v1/fighters/:id/stats", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderStatsPage(1);

    expect(
      await screen.findByText(
        /não foi possível carregar o perfil de striking/i,
      ),
    ).toBeInTheDocument();
  });

  it("volta para a página do lutador pelo link do cabeçalho", async () => {
    const { router } = renderStatsPage(1);

    const back = await screen.findByRole("link", { name: /jon jones/i });
    await userEvent.setup().click(back);

    expect(router.state.location.pathname).toBe("/fighters/1");
  });

  it("mostra mensagem própria quando o lutador não tem lutas agregadas", async () => {
    serveHistory([]);

    renderStatsPage(1);

    // O cabeçalho ainda carrega; o corpo explica a ausência de estatísticas.
    expect(
      await screen.findByRole("heading", { name: /jon jones/i }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/ainda não há estatísticas/i),
    ).toBeInTheDocument();
  });

  it("mostra o skeleton enquanto as estatísticas carregam", () => {
    server.use(
      http.get("*/api/v1/fighters/:id/bouts", async () => {
        await delay("infinite");
        return HttpResponse.json([]);
      }),
    );

    renderStatsPage(1);

    expect(
      screen.getByLabelText(/carregando estatísticas/i),
    ).toBeInTheDocument();
  });

  it("mostra mensagem legível quando o backend falha nas estatísticas", async () => {
    server.use(
      http.get("*/api/v1/fighters/:id/bouts", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderStatsPage(1);

    expect(
      await screen.findByText(/não foi possível carregar as estatísticas/i),
    ).toBeInTheDocument();
  });

  it("mostra 'não encontrado' quando o lutador não existe (404)", async () => {
    renderStatsPage(999);

    expect(
      await screen.findByText(/lutador não encontrado/i),
    ).toBeInTheDocument();
  });

  it("trata id de rota inválido como não encontrado, sem disparar request", async () => {
    let requested = false;
    server.use(
      http.get("*/api/v1/fighters/:id/bouts", () => {
        requested = true;
        return HttpResponse.json([]);
      }),
    );

    renderStatsPage("abc");

    expect(
      await screen.findByText(/lutador não encontrado/i),
    ).toBeInTheDocument();
    expect(requested).toBe(false);
  });

  it("navega da página do lutador para as estatísticas pelo link", async () => {
    const user = userEvent.setup();
    const { router } = renderWithProviders(<FighterPage />, {
      routes: [
        { path: "/fighters/:id", element: <FighterPage /> },
        { path: "/fighters/:id/stats", element: <FighterStatsPage /> },
      ],
      initialEntries: ["/fighters/1"],
    });

    const link = await screen.findByRole("link", { name: /estatísticas/i });
    await user.click(link);

    expect(router.state.location.pathname).toBe("/fighters/1/stats");
    expect(await screen.findByText("60,0")).toBeInTheDocument();
  });
});
