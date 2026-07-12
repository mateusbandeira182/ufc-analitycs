import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { HeadToHeadOut } from "@/api/schema";
import { HeadToHeadPage } from "@/features/head-to-head/HeadToHeadPage";
import { server } from "@/mocks/server";
import { renderWithProviders } from "@/test/renderWithProviders";

function renderHeadToHead(initialEntry = "/head-to-head") {
  return renderWithProviders(<HeadToHeadPage />, {
    routes: [{ path: "/head-to-head", element: <HeadToHeadPage /> }],
    initialEntries: [initialEntry],
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

describe("HeadToHeadPage — seleção e URL", () => {
  it("reflete os dois lutadores escolhidos na URL como ?a=&b=", async () => {
    const user = userEvent.setup();
    const { router } = renderHeadToHead();

    await pickFighter(user, /lutador a/i, "jon", /jon jones/i);
    await pickFighter(user, /lutador b/i, "volk", /alexander volkanovski/i);

    const search = router.state.location.search;
    expect(search).toContain("a=1");
    expect(search).toContain("b=2");
  });
});

describe("HeadToHeadPage — comparação e confrontos", () => {
  it("exibe cartel e atributos dos dois lutadores lado a lado", async () => {
    renderHeadToHead("/head-to-head?a=1&b=2");

    // Cartel V/D/E de cada lado.
    expect(await screen.findByText("27-1-0")).toBeInTheDocument();
    expect(screen.getByText("26-4-0")).toBeInTheDocument();

    // Atributos formatados (alturas distintas identificam cada coluna).
    expect(screen.getByText("193 cm")).toBeInTheDocument();
    expect(screen.getByText("168 cm")).toBeInTheDocument();

    // Nomes dos dois lutadores presentes na comparação.
    expect(
      screen.getByRole("heading", { name: /jon jones/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /alexander volkanovski/i }),
    ).toBeInTheDocument();
  });

  it("lista o confronto direto com resultado e link para /bouts/:id", async () => {
    renderHeadToHead("/head-to-head?a=1&b=2");

    const bout = await screen.findByRole("listitem", {
      name: /ufc 200/i,
    });
    expect(within(bout).getByText("09/07/2016")).toBeInTheDocument();
    expect(within(bout).getByText("KO/TKO")).toBeInTheDocument();
    // Jon Jones (winner_id 1) é o vencedor do confronto.
    expect(within(bout).getByText(/jon jones/i)).toBeInTheDocument();

    const link = within(bout).getByRole("link", { name: /ver luta/i });
    expect(link).toHaveAttribute("href", "/bouts/500");
  });

  it("indica 'nunca se enfrentaram' quando não há confronto direto", async () => {
    server.use(
      http.get("*/api/v1/head-to-head", ({ request }) => {
        const query = new URL(request.url).searchParams;
        const body: HeadToHeadOut = {
          fighter_a_id: Number(query.get("a")),
          fighter_b_id: Number(query.get("b")),
          bouts: [],
        };
        return HttpResponse.json(body);
      }),
    );

    renderHeadToHead("/head-to-head?a=1&b=3");

    expect(
      await screen.findByText(/nunca se enfrentaram/i),
    ).toBeInTheDocument();
    // A comparação de atributos continua visível.
    expect(screen.getByText("27-1-0")).toBeInTheDocument();
  });
});

describe("HeadToHeadPage — carregamento, erro e a == b", () => {
  it("mostra o skeleton enquanto o confronto carrega", () => {
    server.use(
      http.get("*/api/v1/head-to-head", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );

    renderHeadToHead("/head-to-head?a=1&b=2");

    expect(screen.getByLabelText(/carregando confronto/i)).toBeInTheDocument();
  });

  it("mostra 'lutador não encontrado' quando um id da URL não existe (404)", async () => {
    renderHeadToHead("/head-to-head?a=999&b=2");

    expect(
      await screen.findByText(/lutador não encontrado/i),
    ).toBeInTheDocument();
  });

  it("barra a == b com mensagem e não dispara a query de confronto", async () => {
    let requested = false;
    server.use(
      http.get("*/api/v1/head-to-head", () => {
        requested = true;
        return HttpResponse.json({ detail: "não deveria ser chamado" });
      }),
    );

    renderHeadToHead("/head-to-head?a=1&b=1");

    expect(
      await screen.findByText(/selecione dois lutadores distintos/i),
    ).toBeInTheDocument();
    // A guarda client-side evita a ida à rede.
    await waitFor(() => {
      expect(requested).toBe(false);
    });
  });
});
