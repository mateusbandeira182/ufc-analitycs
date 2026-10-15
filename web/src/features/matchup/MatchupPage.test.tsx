import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { MatchupPredictionOut } from "@/api/schema";
import { MatchupPage } from "@/features/matchup/MatchupPage";
import { FRESH_FEATURES } from "@/mocks/fighters";
import { server } from "@/mocks/server";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderMatchup() {
  return renderWithProviders(<MatchupPage />, {
    routes: [{ path: "/matchup", element: <MatchupPage /> }],
    initialEntries: ["/matchup"],
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

describe("MatchupPage — palpite feliz", () => {
  it("mostra o vencedor previsto em destaque e as duas probabilidades", async () => {
    const user = userEvent.setup();
    renderMatchup();

    await pickFighter(user, /lutador a/i, "jon", /jon jones/i);
    await pickFighter(user, /lutador b/i, "volk", /alexander volkanovski/i);
    await user.click(screen.getByRole("button", { name: /prever/i }));

    // Vencedor previsto (Jon Jones, 27 vitórias vs 26): destaque com o nome.
    const winner = await screen.findByRole("status", {
      name: /vencedor previsto/i,
    });
    expect(
      within(winner).getByRole("heading", { name: /jon jones/i }),
    ).toBeInTheDocument();

    // Barras de probabilidade complementares (27/53 ~ 51% e 49%).
    expect(screen.getByText("51%")).toBeInTheDocument();
    expect(screen.getByText("49%")).toBeInTheDocument();
  });

  it("desabilita o botão prever enquanto faltar um dos lados", async () => {
    const user = userEvent.setup();
    renderMatchup();

    expect(screen.getByRole("button", { name: /prever/i })).toBeDisabled();

    await pickFighter(user, /lutador a/i, "jon", /jon jones/i);
    expect(screen.getByRole("button", { name: /prever/i })).toBeDisabled();

    await pickFighter(user, /lutador b/i, "volk", /alexander volkanovski/i);
    expect(screen.getByRole("button", { name: /prever/i })).toBeEnabled();
  });
});

describe("MatchupPage — carregamento e erros", () => {
  it("mostra o indicador de carregamento enquanto o palpite é calculado", async () => {
    server.use(
      http.get("*/api/v1/predict/matchup", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );
    const user = userEvent.setup();
    renderMatchup();

    await pickFighter(user, /lutador a/i, "jon", /jon jones/i);
    await pickFighter(user, /lutador b/i, "volk", /alexander volkanovski/i);
    await user.click(screen.getByRole("button", { name: /prever/i }));

    expect(
      await screen.findByLabelText(/calculando palpite/i),
    ).toBeInTheDocument();
  });

  it("trata o 503 com mensagem de modelo indisponível", async () => {
    server.use(
      http.get("*/api/v1/predict/matchup", () =>
        HttpResponse.json(
          { detail: "Modelo preditivo indisponível." },
          { status: 503 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderMatchup();

    await pickFighter(user, /lutador a/i, "jon", /jon jones/i);
    await pickFighter(user, /lutador b/i, "volk", /alexander volkanovski/i);
    await user.click(screen.getByRole("button", { name: /prever/i }));

    expect(
      await screen.findByText(/modelo indisponível no momento/i),
    ).toBeInTheDocument();
  });

  it("trata o 422 (mesmo lutador dos dois lados) pedindo dois distintos", async () => {
    const user = userEvent.setup();
    renderMatchup();

    await pickFighter(user, /lutador a/i, "jon", /jon jones/i);
    await pickFighter(user, /lutador b/i, "jon", /jon jones/i);
    await user.click(screen.getByRole("button", { name: /prever/i }));

    expect(
      await screen.findByText(/escolha dois lutadores diferentes/i),
    ).toBeInTheDocument();
  });

  it("trata o erro genérico (ex.: 500) sem quebrar a tela", async () => {
    server.use(
      http.get("*/api/v1/predict/matchup", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    renderMatchup();

    await pickFighter(user, /lutador a/i, "jon", /jon jones/i);
    await pickFighter(user, /lutador b/i, "volk", /alexander volkanovski/i);
    await user.click(screen.getByRole("button", { name: /prever/i }));

    expect(
      await screen.findByText(/não foi possível gerar o palpite/i),
    ).toBeInTheDocument();
  });
});

describe("MatchupPage — previsão indisponível", () => {
  it("mostra o motivo e não exibe 0% quando o estreante responde 200 sem probabilidade", async () => {
    /*
      Desde a Sprint 05 (D-04) o estreante não é mais 422: o endpoint responde 200
      com as três probabilidades nulas e o motivo em `unavailable_reason` +
      `unavailable_code`. A asserção negativa de "0%" é obrigatória —
      `clampProbability` mapeia valor não finito para 0, então sem ela a tela
      afirmaria uma medição que o modelo não fez.
    */
    server.use(
      http.get("*/api/v1/predict/matchup", () => {
        const body: MatchupPredictionOut = {
          fighter_a: { id: 1, name: "Jon Jones" },
          fighter_b: { id: 2, name: "Alexander Volkanovski" },
          prob_a_wins: null,
          prob_b_wins: null,
          predicted_winner_id: null,
          unavailable_reason:
            "Sem histórico no acervo para o lutador 2 — o modelo não tem o que medir.",
          unavailable_code: "missing_history",
          blocks: [],
          attributes: [],
          features_freshness: FRESH_FEATURES,
        };
        return HttpResponse.json(body);
      }),
    );
    const user = userEvent.setup();
    renderMatchup();

    await pickFighter(user, /lutador a/i, "jon", /jon jones/i);
    await pickFighter(user, /lutador b/i, "volk", /alexander volkanovski/i);
    await user.click(screen.getByRole("button", { name: /prever/i }));

    expect(
      await screen.findByText(/sem histórico no acervo/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("status", { name: /vencedor previsto/i }),
    ).not.toBeInTheDocument();
  });
});
