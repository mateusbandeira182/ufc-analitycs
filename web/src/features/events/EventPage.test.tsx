import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { MATERIALIZE_ASOF_COMMAND } from "@/components/StaleFeaturesBadge";
import { EventPage } from "@/features/events/EventPage";
import { EventsPage } from "@/features/events/EventsPage";
import {
  CARD_PREDICTION_WITH_FOREIGN_PROSE,
  EVENT_PREDICTION_FIXTURES,
  STALE_CARD_PREDICTION,
  cardPredictionWithCode,
} from "@/mocks/eventPrediction";
import { EVENT_DETAIL_FIXTURES, eventDetailSemCantoAzul } from "@/mocks/events";
import { server } from "@/mocks/server";
import { contarColunas, gradeDeCantos } from "@/test/gridColumns";
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
  /**
   * A mesma invariante que a Home passou a guardar, aqui onde ela é **mais**
   * frágil: o `EventBoutCard` fecha a contagem por um ternário dentro do `.map()`
   * (`index > 0 ? <VersusDivider /> : null`), não por um número estático. Quem
   * remover ou condicionar diferente aquele ternário põe um canto na coluna
   * `auto` — e o jsdom não computa layout, então nada mais falharia.
   */
  it("declara tantas colunas quantos filhos na grade do card de luta", async () => {
    renderEventPage(801);

    const [primeira] = await screen.findAllByRole("listitem");
    const grade = gradeDeCantos(primeira!);

    expect(grade).not.toBeNull();
    expect(contarColunas(grade!.className)).toBe(grade!.children.length);
  });

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

/*
  Card de evento futuro. O modo de cada luta vem do seu próprio `status`
  (RF-31 / D-14), nunca da data do evento: a janela entre o evento acontecer e
  alguém rodar o `gap_sync` (manual) dura dias, e pela data seis eventos reais
  renderizariam "realizado vazio".
*/
describe("EventPage — a requisição que não pode disparar", () => {
  it("não chama /predict/event no card inteiramente realizado", async () => {
    /*
      `GET /predict/event/{id}` ESCREVE em `bout_predictions` com `source="api"`
      e commita, e `biggest_upset` lê a tabela. Um render desta página gravaria
      palpite as-of-hoje sobre luta passada e mudaria a "maior zebra" e o
      denominador do evento e da Home — defeito que falharia calando, com a tela
      bonita e um destaque histórico reescrito.
    */
    let chamadas = 0;
    server.use(
      http.get("*/api/v1/predict/event/:id", () => {
        chamadas += 1;
        return HttpResponse.json(EVENT_PREDICTION_FIXTURES[42]);
      }),
    );

    renderEventPage(42);

    // Espera o card REALIZADO aparecer antes de afirmar a ausência: asserir
    // contra o skeleton de carregamento já deu falso verde nesta SPEC.
    expect(await screen.findByText(/3 lutas realizadas/i)).toBeInTheDocument();
    expect(chamadas).toBe(0);
  });

  it("chama /predict/event quando o card tem luta agendada", async () => {
    let chamadas = 0;
    server.use(
      http.get("*/api/v1/predict/event/:id", () => {
        chamadas += 1;
        return HttpResponse.json(EVENT_PREDICTION_FIXTURES[801]);
      }),
    );

    serveDetail(801);
    renderEventPage(801);

    await screen.findByRole("group", { name: /probabilidade de vitória/i });
    expect(chamadas).toBe(1);
  });

  it("não chama /predict/event no evento sem nenhuma luta", async () => {
    // Sem luta não existe `status` para consultar, e o `UFC 335` está no acervo
    // sem card publicado.
    let chamadas = 0;
    server.use(
      http.get("*/api/v1/predict/event/:id", () => {
        chamadas += 1;
        return HttpResponse.json(EVENT_PREDICTION_FIXTURES[42]);
      }),
    );

    serveDetail(813);
    renderEventPage(813);

    expect(
      await screen.findByText(/card ainda não publicado/i),
    ).toBeInTheDocument();
    expect(chamadas).toBe(0);
  });
});

describe("EventPage — evento de data passada ainda não sincronizado", () => {
  it("renderiza como futuro, com palpite, e não como realizado vazio", async () => {
    serveDetail(801);

    renderEventPage(801);

    // A data do `UFC 332` já passou, mas a luta segue `scheduled`: o palpite
    // continua na tela até o resultado chegar.
    const luta = await screen.findByRole("listitem", {
      name: /belal muhammad vs shavkat rakhmonov/i,
    });
    // O palpite vem de uma SEGUNDA requisição: a espera é por ele, não pela
    // luta — o card já está na tela antes de a previsão chegar.
    const palpite = await within(luta).findByRole("group", {
      name: /probabilidade de vitória/i,
    });
    expect(within(palpite).getByText("58%")).toBeInTheDocument();
    expect(within(palpite).getByText(/belal muhammad/i)).toBeInTheDocument();
    expect(within(luta).getByText(/agendada/i)).toBeInTheDocument();
    // Nenhum canto é rotulado vencedor: a luta não aconteceu.
    expect(within(luta).queryByText(/^vencedor$/i)).not.toBeInTheDocument();
  });
});

describe("EventPage — card misto", () => {
  it("renderiza cada luta pelo seu próprio status", async () => {
    serveDetail(812);

    renderEventPage(812);

    // A realizada: método, momento e vencedor — exatamente como hoje.
    const realizada = await screen.findByRole("listitem", {
      name: /alex pereira vs jamahal hill/i,
    });
    expect(within(realizada).getByText("KO/TKO")).toBeInTheDocument();
    expect(within(realizada).getByText("R1 1:12")).toBeInTheDocument();
    expect(
      within(realizada).getByRole("link", { name: /alex pereira, vencedor/i }),
    ).toBeInTheDocument();

    // A agendada da mesma seção: palpite, sem método e sem vencedor.
    const agendada = within(
      await screen.findByRole("region", { name: /main card/i }),
    ).getByRole("listitem", { name: /charles oliveira vs arman tsarukyan/i });
    expect(
      await within(agendada).findByRole("group", {
        name: /probabilidade de vitória/i,
      }),
    ).toBeInTheDocument();
    expect(within(agendada).getByText(/agendada/i)).toBeInTheDocument();
  });

  it("não exibe como palpite a previsão que veio para a luta já realizada", async () => {
    /*
      `predict_event_card` reusa `list_event_bouts`, que não filtra `status`:
      a luta realizada TAMBÉM volta com entrada de previsão no payload. Decidir
      pela existência da entrada, em vez de pelo `status`, poria um palpite
      as-of-hoje sobre um resultado que já existe.
    */
    serveDetail(812);

    renderEventPage(812);

    const realizada = await screen.findByRole("listitem", {
      name: /alex pereira vs jamahal hill/i,
    });
    // A previsão do card chegou — a vizinha agendada a exibe —, então a ausência
    // aqui é decisão pelo `status`, não falta de dado.
    await within(
      screen.getByRole("listitem", {
        name: /charles oliveira vs arman tsarukyan/i,
      }),
    ).findByRole("group", { name: /probabilidade de vitória/i });
    expect(
      within(realizada).queryByRole("group", {
        name: /probabilidade de vitória/i,
      }),
    ).not.toBeInTheDocument();
    expect(within(realizada).queryByText("71%")).not.toBeInTheDocument();
  });

  it("não particiona nem reordena o card por status", async () => {
    serveDetail(812);

    renderEventPage(812);

    // A seção vem do payload e a ordem é a do servidor. Agrupar as agendadas à
    // parte pareceria organização e quebraria a ordem real do card.
    const principal = await screen.findByRole("region", {
      name: /main card/i,
    });
    const lutas = within(principal).getAllByRole("listitem");
    expect(lutas).toHaveLength(2);
    expect(lutas[0]).toHaveTextContent(/alex pereira/i);
    expect(lutas[1]).toHaveTextContent(/charles oliveira/i);

    const preliminares = screen.getByRole("region", { name: /prelims/i });
    expect(
      within(preliminares).getByRole("listitem", { name: /caio borralho/i }),
    ).toBeInTheDocument();
  });

  it("nunca renderiza `bout_order` como número na tela", async () => {
    serveDetail(812);

    renderEventPage(812);

    await screen.findByRole("region", { name: /main card/i });
    // A luta principal é o MENOR valor (1001) e a faixa não é sequencial nem
    // densa: só a ordem relativa é utilizável.
    for (const valor of ["1001", "1002", "2001"]) {
      expect(screen.queryByText(valor)).not.toBeInTheDocument();
    }
  });
});

describe("EventPage — o estreante no card", () => {
  it("mostra o aviso da ausência de histórico, sem barra e sem porcentagem", async () => {
    serveDetail(812);

    renderEventPage(812);

    const luta = await screen.findByRole("listitem", {
      name: /caio borralho vs dakota ferreira/i,
    });
    expect(
      await within(luta).findByText(/sem histórico para prever/i),
    ).toBeInTheDocument();
    /*
      Nunca 50/50 nem 0%: 50/50 se lê como opinião do modelo quando é ausência
      dela (RF-21), e `clampProbability(null)` renderizaria "0%" — afirmação
      sobre uma luta que ninguém mediu.
    */
    expect(within(luta).queryByText(/\d+\s*%/)).not.toBeInTheDocument();
    expect(
      within(luta).queryByRole("group", { name: /probabilidade de vitória/i }),
    ).not.toBeInTheDocument();
  });

  it("alcança os três códigos de indisponibilidade no card", async () => {
    // No card, ao contrário do matchup isolado, `missing_corners` e
    // `repeated_fighter` também são alcançáveis: são estados do próprio card.
    const avisos = new Set<string>();

    for (const code of [
      "missing_history",
      "repeated_fighter",
      "missing_corners",
    ] as const) {
      server.use(
        http.get("*/api/v1/events/:id", () =>
          HttpResponse.json(
            code === "missing_corners"
              ? eventDetailSemCantoAzul()
              : EVENT_DETAIL_FIXTURES[812],
          ),
        ),
        http.get("*/api/v1/predict/event/:id", () =>
          HttpResponse.json(cardPredictionWithCode(code)),
        ),
      );

      const { unmount } = renderEventPage(812);
      const luta = await screen.findByRole("listitem", {
        name: /caio borralho/i,
      });
      const aviso = await within(luta).findByRole("status");
      avisos.add(aviso.textContent ?? "");
      expect(within(luta).queryByText(/\d+\s*%/)).not.toBeInTheDocument();
      unmount();
    }

    expect(avisos.size).toBe(3);
  });

  it("escolhe o aviso pelo código, não pela prosa", async () => {
    /*
      A prova da DT-6: a fixture troca o texto de `unavailable_reason` por um
      arbitrário mantendo o `unavailable_code`. Falha se algum caminho da tela
      derivar a causa parseando a prosa.
    */
    serveDetail(812);
    server.use(
      http.get("*/api/v1/predict/event/:id", () =>
        HttpResponse.json(CARD_PREDICTION_WITH_FOREIGN_PROSE),
      ),
    );

    renderEventPage(812);

    const luta = await screen.findByRole("listitem", {
      name: /caio borralho/i,
    });
    expect(
      await within(luta).findByText(/sem histórico para prever/i),
    ).toBeInTheDocument();
    expect(
      within(luta).getByText(/prosa completamente diferente/i),
    ).toBeInTheDocument();
  });
});

describe("EventPage — o link que explica o palpite", () => {
  it("leva a /confronto montado pelos ids dos dois cantos", async () => {
    serveDetail(812);

    renderEventPage(812);

    // Montado por `fighter_id`, nunca por nome: um `Map` por nome foi
    // exatamente o defeito que fez toda luta sair "Perdeu" na Sprint 08.
    const agendada = await screen.findByRole("listitem", {
      name: /charles oliveira vs arman tsarukyan/i,
    });
    expect(
      await within(agendada).findByRole("link", { name: /por que/i }),
    ).toHaveAttribute("href", "/confronto?a=300&b=400");
  });

  it("explica também a luta que o modelo não previu", async () => {
    serveDetail(812);

    renderEventPage(812);

    const estreante = await screen.findByRole("listitem", {
      name: /caio borralho/i,
    });
    expect(
      await within(estreante).findByRole("link", { name: /por que/i }),
    ).toHaveAttribute("href", "/confronto?a=900&b=910");
  });

  it("omite o link quando a luta não tem os dois cantos cadastrados", async () => {
    // `/confronto` exige dois lutadores: sem o canto azul não há para onde
    // apontar, e um link quebrado é pior que a ausência dele.
    server.use(
      http.get("*/api/v1/events/:id", () =>
        HttpResponse.json(eventDetailSemCantoAzul()),
      ),
      http.get("*/api/v1/predict/event/:id", () =>
        HttpResponse.json(cardPredictionWithCode("missing_corners")),
      ),
    );

    renderEventPage(812);

    const luta = await screen.findByRole("listitem", {
      name: /caio borralho/i,
    });
    // Espera o aviso aparecer antes de afirmar a ausência do link: asserir
    // contra o estado de carregamento já deu falso verde nesta SPEC. Pelo
    // cabeçalho, porque a prosa do backend repete a mesma frase.
    expect(
      await within(luta).findByRole("heading", {
        name: /sem os dois cantos/i,
      }),
    ).toBeInTheDocument();
    expect(
      within(luta).queryByRole("link", { name: /por que/i }),
    ).not.toBeInTheDocument();
  });

  it("não oferece o link na luta já realizada", async () => {
    serveDetail(812);

    renderEventPage(812);

    const realizada = await screen.findByRole("listitem", {
      name: /alex pereira vs jamahal hill/i,
    });
    // A previsão do card JÁ chegou (a luta agendada vizinha a exibe): a ausência
    // aqui é decisão pelo `status`, não efeito de ainda não ter carregado.
    await within(
      screen.getByRole("listitem", {
        name: /charles oliveira vs arman tsarukyan/i,
      }),
    ).findByRole("group", { name: /probabilidade de vitória/i });
    expect(
      within(realizada).queryByRole("link", { name: /por que/i }),
    ).not.toBeInTheDocument();
  });
});

describe("EventPage — defasagem do cache e retrato no card agendado", () => {
  it("renderiza o aviso de dado defasado na seção do card previsto", async () => {
    serveDetail(812);
    server.use(
      http.get("*/api/v1/predict/event/:id", () =>
        HttpResponse.json(STALE_CARD_PREDICTION),
      ),
    );

    renderEventPage(812);

    // O aviso vive na seção que lê o dado defasado, nunca num banner global:
    // foi assim que o cache por luta escondeu 21 features por meses.
    expect(await screen.findByText(/dado defasado/i)).toBeInTheDocument();
    expect(screen.getByText(MATERIALIZE_ASOF_COMMAND)).toBeInTheDocument();
  });

  it("não renderiza o aviso quando o cache está em dia", async () => {
    serveDetail(812);

    renderEventPage(812);

    await screen.findByRole("region", { name: /main card/i });
    expect(screen.queryByText(/dado defasado/i)).not.toBeInTheDocument();
  });

  it("cai no avatar de iniciais no canto agendado sem retrato", async () => {
    serveDetail(812);

    renderEventPage(812);

    const luta = await screen.findByRole("listitem", {
      name: /caio borralho vs dakota ferreira/i,
    });
    expect(within(luta).getByText("CB")).toBeInTheDocument();
    expect(within(luta).getByText("DF")).toBeInTheDocument();
    expect(
      within(luta).queryByRole("img", { name: /caio borralho/i }),
    ).not.toBeInTheDocument();
  });
});

describe("EventPage — estados da previsão do card", () => {
  it("avisa enquanto as previsões carregam, sem bloquear o card", async () => {
    serveDetail(801);
    server.use(
      http.get("*/api/v1/predict/event/:id", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    renderEventPage(801);

    // O card continua navegável: quem não carregou foi o palpite.
    expect(
      await screen.findByText(/carregando as previsões do card/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", {
        name: /belal muhammad vs shavkat rakhmonov/i,
      }),
    ).toBeInTheDocument();
  });

  it("segue mostrando o card quando a previsão falha, sem barra nem 50/50", async () => {
    serveDetail(801);
    server.use(
      http.get("*/api/v1/predict/event/:id", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderEventPage(801);

    expect(
      await screen.findByText(/não foi possível carregar as previsões/i),
    ).toBeInTheDocument();
    const luta = screen.getByRole("listitem", {
      name: /belal muhammad vs shavkat rakhmonov/i,
    });
    expect(within(luta).getByText(/agendada/i)).toBeInTheDocument();
    expect(within(luta).queryByText(/\d+\s*%/)).not.toBeInTheDocument();
    expect(
      within(luta).queryByRole("group", { name: /probabilidade de vitória/i }),
    ).not.toBeInTheDocument();
  });
});
