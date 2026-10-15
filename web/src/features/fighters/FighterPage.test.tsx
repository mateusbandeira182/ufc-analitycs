import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { BoutMethod, FighterBoutOut, FighterStatsOut } from "@/api/schema";
import { FighterPage } from "@/features/fighters/FighterPage";
import { FightersPage } from "@/features/fighters/FightersPage";
import { BOUTS_WITH_NULL_OPPONENT } from "@/mocks/bouts";
import {
  FIGHTER_STATS_FIXTURES,
  NEVER_MATERIALIZED_FEATURES,
  STALE_FEATURES,
} from "@/mocks/fighters";
import { NULL_STRIKE_SPLITS } from "@/mocks/strikeSplits";
import { server } from "@/mocks/server";
import { fighterStatsRedirect } from "@/router";
import { renderWithProviders } from "@/test/renderWithProviders";

/*
  A página do lutador é uma só desde a Slice 06: cabeçalho, CARREIRA, FORMA
  ATUAL, "Como vence", perfil de striking, o recorte client-side de últimas N
  lutas e o histórico em tabela. As três famílias de número convivem na mesma
  tela, então quase toda asserção aqui é escopada por `within(seção)` — uma busca
  global por "60,0" encontraria qualquer uma das três e passaria por acidente,
  provando o contrário do que o teste afirma.
*/

function renderFighterPage(id: number | string) {
  return renderWithProviders(<FighterPage />, {
    routes: [{ path: "/fighters/:id", element: <FighterPage /> }],
    initialEntries: [`/fighters/${String(id)}`],
  });
}

/**
 * Sobrescreve a resposta de `/fighters/:id/stats` a partir da fixture do lutador
 * 1, para exercitar os estados do bloco as-of sem tocar nas demais seções.
 */
function serveStats(overrides: Partial<FighterStatsOut>) {
  const base = FIGHTER_STATS_FIXTURES[1];
  if (base === undefined) {
    throw new Error("fixture de estatísticas do lutador 1 ausente");
  }
  const body: FighterStatsOut = { ...base, ...overrides };
  server.use(
    http.get("*/api/v1/fighters/:id/stats", () => HttpResponse.json(body)),
  );
}

/** Substitui o histórico servido para o lutador nos testes que precisam de um recorte. */
function serveHistory(bouts: FighterBoutOut[]) {
  server.use(
    http.get("*/api/v1/fighters/:id/bouts", () => HttpResponse.json(bouts)),
  );
}

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

/**
 * A seção do recorte client-side, cujo nome acessível muda com o filtro.
 * Assíncrona porque a seção só existe depois que o lutador carrega — antes
 * disso a página inteira é um skeleton.
 */
function recorte(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: /recorte/i });
}

describe("FighterPage — cabeçalho e cartel", () => {
  it("exibe o nome e o cartel V/D/E do lutador no sucesso", async () => {
    renderFighterPage(1);

    expect(
      await screen.findByRole("heading", { name: /jon jones/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("27-1-0")).toBeInTheDocument();
  });

  it("exibe o peso do lutador no cabeçalho", async () => {
    renderFighterPage(1);

    // Jon Jones (fixture): 93 kg.
    expect(await screen.findByText("Peso")).toBeInTheDocument();
    expect(screen.getByText("93 kg")).toBeInTheDocument();
  });

  it("prefere a imagem de corpo inteiro ao retrato", async () => {
    renderFighterPage(1);

    await screen.findByRole("heading", { name: /jon jones/i });
    expect(screen.getByRole("img", { name: /jon jones/i })).toHaveAttribute(
      "src",
      "https://imagens.ufc.test/jon-jones-corpo.png",
    );
  });

  it("cai nas iniciais quando o lutador não tem nenhuma das duas imagens", async () => {
    // Volkanovski (id 2): sem retrato e sem corpo inteiro — o caso comum do
    // acervo, que não pode parecer um buraco na tela.
    renderFighterPage(2);

    await screen.findByRole("heading", { name: /alexander volkanovski/i });
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText("AV")).toBeInTheDocument();
  });

  it("não oferece mais link para uma página de estatísticas separada", async () => {
    renderFighterPage(1);

    await screen.findByRole("heading", { name: /jon jones/i });
    expect(
      screen.queryByRole("link", { name: /estatísticas do atleta/i }),
    ).not.toBeInTheDocument();
  });
});

describe("FighterPage — CARREIRA", () => {
  it("mostra as médias de carreira que o servidor agrega", async () => {
    renderFighterPage(1);

    const carreira = await screen.findByRole("region", { name: /carreira/i });
    expect(await within(carreira).findByText("60,0")).toBeInTheDocument();
    expect(within(carreira).getByText("1,0")).toBeInTheDocument();
    expect(within(carreira).getByText("3:24")).toBeInTheDocument();
    expect(
      within(carreira).getByText(/2 lutas contabilizadas/i),
    ).toBeInTheDocument();
  });

  it("separa o jogo em pé do jogo no chão (striker vs grappler)", async () => {
    renderFighterPage(1);

    const carreira = await screen.findByRole("region", { name: /carreira/i });
    expect(await within(carreira).findByText(/em pé/i)).toBeInTheDocument();
    expect(within(carreira).getByText(/no chão/i)).toBeInTheDocument();
  });

  it("detalha como vence por método, a partir do agregado do servidor", async () => {
    renderFighterPage(1);

    // O "como vence" de carreira lê `wins_by_method` da resposta do servidor
    // (fixture: uma vitória por nocaute) — nunca o histórico recortado no
    // cliente, que é o do bloco de recorte.
    const breakdown = await screen.findByRole("list", { name: "Como vence" });
    const items = within(breakdown).getAllByRole("listitem");

    expect(items).toHaveLength(1);
    expect(breakdown).toHaveTextContent("KO/TKO");
  });

  it("mostra mensagem própria quando o lutador não tem lutas agregadas", async () => {
    // Adesanya (id 3): a fixture de stats traz `bouts_counted: 0`.
    renderFighterPage(3);

    const carreira = await screen.findByRole("region", { name: /carreira/i });
    expect(
      await within(carreira).findByText(/ainda não há estatísticas/i),
    ).toBeInTheDocument();
  });

  it("mostra mensagem legível quando o agregado falha", async () => {
    server.use(
      http.get("*/api/v1/fighters/:id/stats", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderFighterPage(1);

    const carreira = await screen.findByRole("region", { name: /carreira/i });
    expect(
      await within(carreira).findByText(
        /não foi possível carregar as estatísticas/i,
      ),
    ).toBeInTheDocument();
  });
});

describe("FighterPage — FORMA ATUAL", () => {
  it("mostra os valores as-of da janela de três lutas", async () => {
    renderFighterPage(1);

    const formaAtual = await screen.findByRole("region", {
      name: /forma atual/i,
    });
    const janela = await within(formaAtual).findByRole("group", {
      name: /janela de 3 lutas/i,
    });

    expect(within(janela).getByText("4,2")).toBeInTheDocument();
    expect(within(janela).getByText("2,8")).toBeInTheDocument();
    expect(within(janela).getByText("1,5")).toBeInTheDocument();
    expect(within(janela).getByText("0,6")).toBeInTheDocument();
    expect(within(janela).getByText("3:06")).toBeInTheDocument();
  });

  it("mostra as tendências assinadas, com zero e ausência distintos", async () => {
    renderFighterPage(1);

    const formaAtual = await screen.findByRole("region", {
      name: /forma atual/i,
    });
    const tendencia = await within(formaAtual).findByRole("group", {
      name: /tendência/i,
    });

    // O sinal explícito é o que torna o número legível como comparação.
    expect(within(tendencia).getByText("+0,7")).toBeInTheDocument();
    expect(within(tendencia).getByText("-0,3")).toBeInTheDocument();
    // Janela igual à carreira é medição legítima, não ausência.
    expect(within(tendencia).getByText("0,0")).toBeInTheDocument();
    // Campo nulo no payload: traço, nunca "0,0".
    expect(within(tendencia).getByText("—")).toBeInTheDocument();
    // Legenda neutra: nenhum dos cinco ganha cor ou seta de bom/ruim, porque
    // absorver mais é pior e os outros quatro positivos são melhores (DP-10).
    expect(
      within(tendencia).getByText(/comparado à média de carreira/i),
    ).toBeInTheDocument();
  });

  it("rotula o desgaste acumulado como carreira, não como janela de três lutas", async () => {
    renderFighterPage(1);

    const formaAtual = await screen.findByRole("region", {
      name: /forma atual/i,
    });
    const desgaste = await within(formaAtual).findByRole("group", {
      name: /desgaste acumulado/i,
    });

    expect(within(desgaste).getByText("312,5")).toBeInTheDocument();
    // Contagem de derrota é contagem: "1", nunca "1,0".
    expect(within(desgaste).getByText("1")).toBeInTheDocument();
    expect(within(desgaste).getByText("0")).toBeInTheDocument();
    expect(
      within(desgaste).getByText(/carreira anterior a esta luta/i),
    ).toBeInTheDocument();
  });

  it("declara a falta de histórico quando o lutador não tem linha no cache, sem mostrar zeros", async () => {
    // Adesanya (id 3): a fixture traz `current_form: null` com o cache em dia.
    renderFighterPage(3);

    const formaAtual = await screen.findByRole("region", {
      name: /forma atual/i,
    });
    expect(
      await within(formaAtual).findByText(/sem histórico suficiente/i),
    ).toBeInTheDocument();
    // Ausência declarada, nunca um vetor de zeros: zero em golpe significativo
    // seria uma afirmação sobre o lutador.
    expect(within(formaAtual).queryByText("0,0")).not.toBeInTheDocument();
    // Cache em dia: nenhum aviso de defasagem.
    expect(within(formaAtual).queryByRole("status")).not.toBeInTheDocument();
  });

  it("distingue cache nunca materializado de lutador sem histórico, com o comando", async () => {
    serveStats({
      current_form: null,
      features_freshness: NEVER_MATERIALIZED_FEATURES,
    });
    renderFighterPage(1);

    const formaAtual = await screen.findByRole("region", {
      name: /forma atual/i,
    });
    const aviso = await within(formaAtual).findByRole("status");
    expect(aviso).toHaveTextContent(/nunca foi materializado/i);
    expect(aviso).toHaveTextContent("--stage materialize-asof");
    expect(
      within(formaAtual).getByText(/depois que o cache for materializado/i),
    ).toBeInTheDocument();
  });

  it("mostra o aviso de defasagem dentro da seção FORMA ATUAL, e não fora dela", async () => {
    serveStats({ features_freshness: STALE_FEATURES });
    renderFighterPage(1);

    const formaAtual = await screen.findByRole("region", {
      name: /forma atual/i,
    });
    // O aviso mora na seção que LÊ o dado defasado (RF-06) — nunca num banner
    // global, que o olho aprende a ignorar.
    expect(await within(formaAtual).findByRole("status")).toHaveTextContent(
      /dado defasado/i,
    );

    const carreira = screen.getByRole("region", { name: /carreira/i });
    expect(within(carreira).queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("FighterPage — perfil de striking", () => {
  it("exibe os shares por alvo e por posição", async () => {
    renderFighterPage(1);

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
    renderFighterPage(3);

    // Seis shares (alvo + posição), todos sem dado — nunca NaN/Infinity na tela.
    expect(await screen.findAllByText(/sem dado/i)).toHaveLength(6);
  });
});

describe("FighterPage — recorte de últimas N lutas", () => {
  it("identifica o recorte na tela e muda só os números dele", async () => {
    const user = userEvent.setup();
    // Três lutas de médias distintas: é o que torna "mudou" e "não mudou"
    // distinguíveis. O agregado de CARREIRA vem do servidor (60,0) e não deste
    // histórico, então as duas famílias de número não se confundem.
    serveHistory([
      makeBout({ id: 1, date: "2020-01-01", sig: 10, td: 5, control: 60 }),
      makeBout({ id: 2, date: "2022-01-01", sig: 20, td: 4, control: 120 }),
      makeBout({ id: 3, date: "2024-01-01", sig: 30, td: 3, control: 180 }),
    ]);
    renderFighterPage(1);

    const carreira = await screen.findByRole("region", { name: /carreira/i });
    const formaAtual = screen.getByRole("region", { name: /forma atual/i });
    expect(await within(carreira).findByText("60,0")).toBeInTheDocument();
    expect(
      await within(await recorte()).findByText("20,0"),
    ).toBeInTheDocument();

    await user.click(
      within(
        screen.getByRole("group", { name: /recortar pelas últimas lutas/i }),
      ).getByRole("button", { name: "2" }),
    );

    // O bloco recalculado se identifica: é o preço que a D-13 cobrou por manter
    // o filtro client-side.
    const cortado = await screen.findByRole("region", {
      name: /recorte: últimas 2 lutas/i,
    });
    // Últimas 2 (2022/2024): (20+30)/2 = 25,0.
    expect(within(cortado).getByText("25,0")).toBeInTheDocument();
    expect(
      within(cortado).getByText(/2 lutas contabilizadas/i),
    ).toBeInTheDocument();

    // CARREIRA e FORMA ATUAL leem o servidor e não têm caminho pelo qual o
    // seletor as alcance — a asserção é escopada por seção de propósito.
    expect(within(carreira).getByText("60,0")).toBeInTheDocument();
    expect(
      within(carreira).getByText(/2 lutas contabilizadas/i),
    ).toBeInTheDocument();
    expect(
      within(
        within(formaAtual).getByRole("group", { name: /janela de 3 lutas/i }),
      ).getByText("4,2"),
    ).toBeInTheDocument();
  });

  it("recorta pelas últimas N lutas e desfaz o recorte em 'Todas'", async () => {
    const user = userEvent.setup();
    serveHistory(RICH_HISTORY);
    renderFighterPage(1);

    // Todas as 5 lutas: média de golpes (10+20+30+40+50)/5 = 30,0.
    expect(
      await within(await recorte()).findByText("30,0"),
    ).toBeInTheDocument();
    expect(
      within(await recorte()).getByText(/5 lutas contabilizadas/i),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "3" }));

    // Últimas 3 (2020/2022/2024): (30+40+50)/3 = 40,0.
    expect(
      await within(await recorte()).findByText("40,0"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "3" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await user.click(screen.getByRole("button", { name: "Todas" }));
    expect(
      await within(await recorte()).findByText("30,0"),
    ).toBeInTheDocument();
    expect(
      within(await recorte()).getByText(/todas as lutas/i),
    ).toBeInTheDocument();
  });

  it("recorta por período de datas (De/Até)", async () => {
    serveHistory(RICH_HISTORY);
    renderFighterPage(1);

    await within(await recorte()).findByText(/5 lutas contabilizadas/i);

    fireEvent.change(screen.getByLabelText(/data inicial/i), {
      target: { value: "2018-01-01" },
    });
    fireEvent.change(screen.getByLabelText(/data final/i), {
      target: { value: "2020-12-31" },
    });

    // Período mantém 2018 e 2020: média de golpes (20+30)/2 = 25,0.
    expect(
      await within(await recorte()).findByText(/2 lutas contabilizadas/i),
    ).toBeInTheDocument();
    expect(within(await recorte()).getByText("25,0")).toBeInTheDocument();
  });

  it("combina período e últimas N por interseção", async () => {
    const user = userEvent.setup();
    serveHistory(RICH_HISTORY);
    renderFighterPage(1);

    await within(await recorte()).findByText(/5 lutas contabilizadas/i);

    fireEvent.change(screen.getByLabelText(/data inicial/i), {
      target: { value: "2016-01-01" },
    });
    fireEvent.change(screen.getByLabelText(/data final/i), {
      target: { value: "2020-12-31" },
    });
    await user.click(screen.getByRole("button", { name: "2" }));

    // Período deixa {2016,2018,2020}; as últimas 2 são {2018,2020}: (20+30)/2 = 25,0.
    expect(
      await within(await recorte()).findByText(/2 lutas contabilizadas/i),
    ).toBeInTheDocument();
    expect(within(await recorte()).getByText("25,0")).toBeInTheDocument();
  });

  it("mantém os filtros e avisa quando o recorte não pega nenhuma luta", async () => {
    serveHistory(RICH_HISTORY);
    renderFighterPage(1);

    await within(await recorte()).findByText(/5 lutas contabilizadas/i);

    fireEvent.change(screen.getByLabelText(/data inicial/i), {
      target: { value: "2030-01-01" },
    });

    expect(
      await screen.findByText(/nenhuma luta corresponde ao recorte/i),
    ).toBeInTheDocument();
    // Os filtros seguem visíveis para o usuário ajustar.
    expect(screen.getByRole("button", { name: "Todas" })).toBeInTheDocument();
  });

  it("detalha como venceu dentro do recorte, em seção própria", async () => {
    serveHistory(RICH_HISTORY);
    renderFighterPage(1);

    // O "como venceu" filtrado vive DENTRO da seção do recorte, com cabeçalho
    // próprio: sem o rótulo, ele se leria como o de carreira (RF-24).
    const cortado = await screen.findByRole("list", {
      name: "Como venceu no recorte",
    });
    const items = within(cortado).getAllByRole("listitem");

    // Vitórias no histórico recortado: 2 KO/TKO, 1 finalização, 1 decisão —
    // conjunto diferente do de carreira, servido pela mesma tela.
    expect(items).toHaveLength(3);
    expect(
      within(await recorte()).getByRole("list", {
        name: "Como venceu no recorte",
      }),
    ).toBe(cortado);
  });

  it("substitui a barra por uma nota quando o lutador tem lutas mas nenhuma vitória", async () => {
    serveHistory([
      makeBout({ id: 1, date: "2020-01-01", sig: 30, won: false }),
      makeBout({ id: 2, date: "2021-01-01", sig: 40, won: false }),
    ]);

    renderFighterPage(1);

    expect(
      await within(await recorte()).findByText(/nenhuma vitória registrada/i),
    ).toBeInTheDocument();
    expect(
      within(await recorte()).queryByRole("list", {
        name: "Como venceu no recorte",
      }),
    ).not.toBeInTheDocument();
  });

  it("mostra o skeleton enquanto o recorte carrega", async () => {
    server.use(
      http.get("*/api/v1/fighters/:id/bouts", async () => {
        await delay("infinite");
        return HttpResponse.json([]);
      }),
    );

    renderFighterPage(1);

    expect(
      await screen.findByLabelText(/carregando recorte/i),
    ).toBeInTheDocument();
  });

  it("mostra mensagem legível quando o histórico falha", async () => {
    server.use(
      http.get("*/api/v1/fighters/:id/bouts", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderFighterPage(1);

    expect(
      await within(await recorte()).findByText(
        /não foi possível recompor o recorte/i,
      ),
    ).toBeInTheDocument();
  });

  it("explica a ausência do recorte quando o lutador não tem lutas", async () => {
    serveHistory([]);

    renderFighterPage(1);

    expect(
      await within(await recorte()).findByText(/nenhuma luta registrada/i),
    ).toBeInTheDocument();
  });
});

describe("FighterPage — histórico", () => {
  it("exibe o histórico em tabela, em ordem cronológica, com evento, data, adversário e resultado", async () => {
    renderFighterPage(1);

    const history = await screen.findByRole("table", { name: /histórico/i });
    const rows = within(history).getAllByRole("row");

    // Primeira linha é o cabeçalho da tabela; a ordem cronológica é a que o
    // backend entrega — o cliente não reordena.
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("UFC 152");
    expect(rows[1]).toHaveTextContent("22/09/2012");
    expect(rows[1]).toHaveTextContent("Alexander Volkanovski");
    expect(rows[1]).toHaveTextContent("Derrota");
    expect(rows[1]).toHaveTextContent("Decisão");

    expect(rows[2]).toHaveTextContent("UFC 200");
    expect(rows[2]).toHaveTextContent("09/07/2016");
    expect(rows[2]).toHaveTextContent("Israel Adesanya");
    expect(rows[2]).toHaveTextContent("Vitória");
    expect(rows[2]).toHaveTextContent("KO/TKO");
  });

  it("leva cada linha para a luta, o adversário e o evento", async () => {
    renderFighterPage(1);

    const history = await screen.findByRole("table", { name: /histórico/i });
    const row = within(history).getAllByRole("row")[2];
    if (row === undefined) {
      throw new Error("linha do histórico ausente");
    }

    // Três destinos por linha: antes, o nome do evento levava à luta e não havia
    // link nenhum para o evento.
    expect(
      within(row).getByRole("link", { name: /ver luta/i }),
    ).toHaveAttribute("href", "/bouts/92");
    expect(
      within(row).getByRole("link", { name: /israel adesanya/i }),
    ).toHaveAttribute("href", "/fighters/3");
    expect(within(row).getByRole("link", { name: /ufc 200/i })).toHaveAttribute(
      "href",
      "/events/42",
    );
  });

  it("trata adversário nulo com texto de fallback e sem link", async () => {
    serveHistory(BOUTS_WITH_NULL_OPPONENT);

    renderFighterPage(1);

    expect(
      await screen.findByText(/adversário desconhecido/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /adversário desconhecido/i }),
    ).not.toBeInTheDocument();
  });

  it("mostra mensagem própria quando o histórico está vazio, mantendo o cartel", async () => {
    serveHistory([]);

    renderFighterPage(1);

    expect(
      await screen.findByText(/nenhuma luta registrada para este lutador/i),
    ).toBeInTheDocument();
    expect(screen.getByText("27-1-0")).toBeInTheDocument();
  });

  it("mostra mensagem legível quando o histórico falha", async () => {
    server.use(
      http.get("*/api/v1/fighters/:id/bouts", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderFighterPage(1);

    expect(
      await screen.findByText(/não foi possível carregar o histórico/i),
    ).toBeInTheDocument();
  });
});

describe("FighterPage — navegação e estados da rota", () => {
  it("navega da lista para o detalhe ao clicar num lutador", async () => {
    const user = userEvent.setup();
    const { router } = renderWithProviders(<FightersPage />, {
      routes: [
        { path: "/fighters", element: <FightersPage /> },
        { path: "/fighters/:id", element: <FighterPage /> },
      ],
      initialEntries: ["/fighters"],
    });

    const link = await screen.findByRole("link", { name: /jon jones/i });
    await user.click(link);

    expect(router.state.location.pathname).toBe("/fighters/1");
    expect(
      await screen.findByRole("heading", { name: /jon jones/i }),
    ).toBeInTheDocument();
  });

  it("mostra o skeleton enquanto o lutador carrega", () => {
    server.use(
      http.get("*/api/v1/fighters/:id", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    renderFighterPage(1);

    expect(screen.getByLabelText(/carregando lutador/i)).toBeInTheDocument();
  });

  it("mostra mensagem legível quando o backend falha", async () => {
    server.use(
      http.get("*/api/v1/fighters/:id", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderFighterPage(1);

    expect(
      await screen.findByText(/não foi possível carregar o lutador/i),
    ).toBeInTheDocument();
  });

  it("mostra 'não encontrado' quando o lutador não existe (404)", async () => {
    renderFighterPage(999);

    expect(
      await screen.findByText(/lutador não encontrado/i),
    ).toBeInTheDocument();
  });

  it("trata id de rota inválido como não encontrado, sem disparar request", async () => {
    // Ex.: `/fighters/abc` -> Number("abc") é NaN. O hook fica desabilitado e a
    // página mostra "não encontrado", coerente com /events e /bouts.
    let requested = false;
    server.use(
      http.get("*/api/v1/fighters/:id", () => {
        requested = true;
        return HttpResponse.json({ detail: "Not Found" }, { status: 404 });
      }),
    );

    renderFighterPage("abc");

    expect(
      await screen.findByText(/lutador não encontrado/i),
    ).toBeInTheDocument();
    expect(requested).toBe(false);
  });
});

describe("FighterPage — redirecionamento da rota absorvida", () => {
  /** Monta as duas rotas como o router real as declara, entrando pela antiga. */
  function renderStatsRoute(id: number | string) {
    return renderWithProviders(<FighterPage />, {
      routes: [
        { path: "/fighters/:id", element: <FighterPage /> },
        {
          path: "/fighters/:id/stats",
          loader: fighterStatsRedirect,
        },
      ],
      initialEntries: [`/fighters/${String(id)}/stats`],
    });
  }

  it("redireciona /fighters/:id/stats para a página do lutador", async () => {
    // A tela de estatísticas foi absorvida pela do lutador (RF-24); a rota antiga
    // sobrevive só como redirecionamento, para não quebrar link já salvo.
    const { router } = renderStatsRoute(1);

    expect(
      await screen.findByRole("heading", { name: /jon jones/i }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/fighters/1");
  });

  it("redireciona id inválido e cai em 'não encontrado', sem disparar request", async () => {
    let requested = false;
    server.use(
      http.get("*/api/v1/fighters/:id", () => {
        requested = true;
        return HttpResponse.json({ detail: "Not Found" }, { status: 404 });
      }),
    );

    const { router } = renderStatsRoute("abc");

    expect(
      await screen.findByText(/lutador não encontrado/i),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/fighters/abc");
    expect(requested).toBe(false);
  });
});
