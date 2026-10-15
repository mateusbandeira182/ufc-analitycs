import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ConfrontoPage } from "@/features/confronto/ConfrontoPage";
import { unavailableMatchup } from "@/mocks/matchup";
import { server } from "@/mocks/server";
import { confrontoRedirect } from "@/router";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderConfronto(initialEntry = "/confronto") {
  return renderWithProviders(<ConfrontoPage />, {
    routes: [{ path: "/confronto", element: <ConfrontoPage /> }],
    initialEntries: [initialEntry],
  });
}

/** Monta as três rotas como o router real as declara, entrando por uma antiga. */
function renderRotaAntiga(entrada: string) {
  return renderWithProviders(<ConfrontoPage />, {
    routes: [
      { path: "/confronto", element: <ConfrontoPage /> },
      { path: "/head-to-head", loader: confrontoRedirect },
      { path: "/matchup", loader: confrontoRedirect },
    ],
    initialEntries: [entrada],
  });
}

/** Escolhe um lutador num seletor: digita o termo e clica na opção do dropdown. */
async function pickFighter(
  user: ReturnType<typeof userEvent.setup>,
  label: RegExp,
  term: string,
  optionName: RegExp,
) {
  const input = screen.getByRole("combobox", { name: label });
  await user.type(input, term);
  const option = await screen.findByRole("option", { name: optionName });
  await user.click(option);
}

describe("ConfrontoPage — as cinco seções numa página só", () => {
  it("mostra previsão, influência por bloco, atributos, adversários em comum e confronto direto", async () => {
    renderConfronto("/confronto?a=1&b=2");

    // `findBy*` para o conteúdo carregado: asserir contra o skeleton seria
    // asserir contra o estado de carregamento sem perceber.
    expect(
      await screen.findByRole("region", { name: /previsão do modelo/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: /moveu a agulha/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: /atributos antes da luta/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: /adversários em comum/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: /confronto direto/i }),
    ).toBeInTheDocument();
  });

  it("mostra o palpite, a influência dos blocos e o confronto direto do par", async () => {
    renderConfronto("/confronto?a=1&b=2");

    // Palpite (27/53), um bloco com deslocamento conhecido e a luta do par.
    expect(await screen.findByText("51%")).toBeInTheDocument();
    expect(
      within(
        screen.getByRole("listitem", { name: /perfil de striking/i }),
      ).getByText("+4,1 p.p."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", { name: /ufc 200/i }),
    ).toBeInTheDocument();
  });

  it("mostra o adversário em comum do par que nunca se enfrentou", async () => {
    renderConfronto("/confronto?a=1&b=3");

    expect(
      await screen.findByRole("listitem", { name: /alexander volkanovski/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/nunca se enfrentaram/i)).toBeInTheDocument();
  });
});

describe("ConfrontoPage — a URL é a fonte da verdade da dupla", () => {
  it("escreve o lado escolhido no parâmetro da URL", async () => {
    const user = userEvent.setup();
    const { router } = renderConfronto();

    await pickFighter(user, /lutador a/i, "jon", /jon jones/i);
    await pickFighter(user, /lutador b/i, "volk", /alexander volkanovski/i);

    const search = router.state.location.search;
    expect(search).toContain("a=1");
    expect(search).toContain("b=2");
  });

  it("convida a escolher quando falta um dos lados", async () => {
    renderConfronto("/confronto?a=1");

    expect(await screen.findByText(/escolha os dois/i)).toBeInTheDocument();
  });

  it("barra a == b com mensagem e não dispara NENHUMA das quatro queries", async () => {
    /*
      A guarda é client-side e anterior à rede (CA-P2). É por isso que o 422 do
      endpoint é inalcançável nesta tela, e por isso que o ramo 422 herdado do
      `MatchupPage` não foi portado: seria caminho morto.
    */
    const chamadas: string[] = [];
    server.use(
      http.get("*/api/v1/fighters/:id", () => {
        chamadas.push("fighter");
        return HttpResponse.json({ detail: "não deveria ser chamado" });
      }),
      http.get("*/api/v1/head-to-head", () => {
        chamadas.push("head-to-head");
        return HttpResponse.json({ detail: "não deveria ser chamado" });
      }),
      http.get("*/api/v1/predict/matchup", () => {
        chamadas.push("matchup");
        return HttpResponse.json({ detail: "não deveria ser chamado" });
      }),
    );

    renderConfronto("/confronto?a=1&b=1");

    expect(
      await screen.findByText(/dois atletas diferentes/i),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(chamadas).toEqual([]);
    });
  });
});

describe("ConfrontoPage — carregamento e erros", () => {
  it("mostra o skeleton enquanto o confronto carrega", () => {
    server.use(
      http.get("*/api/v1/head-to-head", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    renderConfronto("/confronto?a=1&b=2");

    expect(screen.getByLabelText(/carregando confronto/i)).toBeInTheDocument();
  });

  it("mostra 'lutador não encontrado' quando um id da URL não existe (404)", async () => {
    renderConfronto("/confronto?a=999&b=2");

    expect(
      await screen.findByText(/lutador não encontrado/i),
    ).toBeInTheDocument();
  });

  it("trata o erro genérico (500) sem quebrar a tela", async () => {
    server.use(
      http.get("*/api/v1/head-to-head", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    renderConfronto("/confronto?a=1&b=2");

    expect(
      await screen.findByText(/não foi possível carregar o confronto/i),
    ).toBeInTheDocument();
  });

  it("503 na previsão informa o modelo fora, e as outras seções seguem renderizando", async () => {
    /*
      Adversários em comum e confronto direto não dependem do modelo: a previsão
      falhar não pode derrubar a página.
    */
    server.use(
      http.get("*/api/v1/predict/matchup", () =>
        HttpResponse.json(
          { detail: "Modelo preditivo indisponível." },
          { status: 503 },
        ),
      ),
    );

    renderConfronto("/confronto?a=1&b=2");

    expect(
      await screen.findByText(/modelo indisponível no momento/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", { name: /ufc 200/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: /adversários em comum/i }),
    ).toBeInTheDocument();
  });

  it("previsão indisponível não derruba a página e não deixa seção vazia na tela", async () => {
    server.use(
      http.get("*/api/v1/predict/matchup", () =>
        HttpResponse.json(unavailableMatchup("missing_history")),
      ),
    );

    renderConfronto("/confronto?a=1&b=2");

    expect(
      await screen.findByRole("heading", {
        name: /sem histórico para prever/i,
      }),
    ).toBeInTheDocument();
    // `blocks` e `attributes` vêm vazios por contrato: as seções não existem.
    expect(
      screen.queryByRole("region", { name: /moveu a agulha/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: /atributos antes da luta/i }),
    ).not.toBeInTheDocument();
    // As que não dependem do modelo continuam de pé.
    expect(
      screen.getByRole("region", { name: /adversários em comum/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("listitem", { name: /ufc 200/i }),
    ).toBeInTheDocument();
  });
});

describe("ConfrontoPage — rotas aposentadas redirecionam (CA-19)", () => {
  it("/head-to-head leva a /confronto preservando os parâmetros", async () => {
    const { router } = renderRotaAntiga("/head-to-head?a=1&b=2");

    expect(await screen.findByText("51%")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/confronto");
    expect(router.state.location.search).toBe("?a=1&b=2");
  });

  it("/matchup leva a /confronto preservando os parâmetros", async () => {
    const { router } = renderRotaAntiga("/matchup?a=1&b=2");

    expect(await screen.findByText("51%")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/confronto");
    expect(router.state.location.search).toBe("?a=1&b=2");
  });

  it("/matchup sem parâmetros leva a /confronto sem query", async () => {
    const { router } = renderRotaAntiga("/matchup");

    expect(
      await screen.findByRole("combobox", { name: /lutador a/i }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/confronto");
    expect(router.state.location.search).toBe("");
  });
});
