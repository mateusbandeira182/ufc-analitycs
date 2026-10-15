import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { UnavailableCode } from "@/api/schema";
import { UnavailablePrediction } from "@/features/confronto/UnavailablePrediction";

const PROSA =
  "Sem histórico de lutas no granular para o(s) lutador(es) de id 2; sem lutas passadas não há features as-of para predizer.";

function renderAviso(code: UnavailableCode | null, reason = PROSA) {
  return render(<UnavailablePrediction code={code} reason={reason} />);
}

/** O texto do aviso, para comparar dois payloads entre si. */
function textoDoAviso(): string {
  return screen.getByRole("status").textContent ?? "";
}

describe("UnavailablePrediction — o código escolhe o aviso", () => {
  it("distingue a luta sem os dois cantos cadastrados", () => {
    renderAviso("missing_corners");

    expect(
      screen.getByRole("heading", { name: /sem os dois cantos/i }),
    ).toBeInTheDocument();
  });

  it("distingue o lutador escalado em mais de uma luta do card", () => {
    renderAviso("repeated_fighter");

    expect(
      screen.getByRole("heading", { name: /mais de uma luta do card/i }),
    ).toBeInTheDocument();
  });

  it("distingue a ausência de histórico no acervo", () => {
    renderAviso("missing_history");

    expect(
      screen.getByRole("heading", { name: /sem histórico para prever/i }),
    ).toBeInTheDocument();
  });

  it("produz três avisos diferentes para os três códigos", () => {
    const avisos = new Set<string>();
    for (const code of [
      "missing_corners",
      "repeated_fighter",
      "missing_history",
    ] as const) {
      const { unmount } = renderAviso(code);
      avisos.add(textoDoAviso());
      unmount();
    }

    expect(avisos.size).toBe(3);
  });
});

describe("UnavailablePrediction — a prosa é exibida, nunca interpretada", () => {
  it("exibe a prosa do backend verbatim, com os ids embutidos", () => {
    renderAviso("missing_history");

    expect(screen.getByText(PROSA)).toBeInTheDocument();
  });

  it("escolhe avisos DIFERENTES para códigos diferentes com a MESMA prosa", () => {
    /*
      A prova de CA-24: é este teste que falha se alguém derivar a causa
      parseando `unavailable_reason`. Nenhum outro o faria — com prosas
      distintas, um parse acertaria por acidente.
    */
    const { unmount } = renderAviso("missing_corners", PROSA);
    const comCorners = textoDoAviso();
    unmount();

    renderAviso("repeated_fighter", PROSA);
    const comRepetido = textoDoAviso();

    expect(comCorners).not.toBe(comRepetido);
  });

  it("escolhe o MESMO aviso para o mesmo código com prosas diferentes", () => {
    // O espelho do teste anterior: muda só a prosa exibida, não o aviso.
    const { unmount } = renderAviso("missing_history", "Prosa de um jeito.");
    const primeiro = screen.getByRole("heading").textContent;
    unmount();

    renderAviso("missing_history", "Prosa de outro jeito completamente.");

    expect(screen.getByRole("heading").textContent).toBe(primeiro);
  });

  it("não quebra quando a prosa não veio", () => {
    renderAviso("missing_history", "");

    expect(screen.getByRole("status")).toBeInTheDocument();
  });
});

describe("UnavailablePrediction — nunca probabilidade, nunca 50/50", () => {
  it("não exibe porcentagem alguma em nenhum dos códigos", () => {
    /*
      CA-15: ausência de palpite é um estado, não um número. 50/50 se lê como
      opinião do modelo quando é ausência dela (RF-21), e `clampProbability(null)`
      renderizaria "0%" — uma afirmação sobre a luta que ninguém mediu.
    */
    for (const code of [
      "missing_corners",
      "repeated_fighter",
      "missing_history",
      null,
    ] as const) {
      const { unmount } = renderAviso(code);
      expect(screen.queryByText(/\d+\s*%/)).not.toBeInTheDocument();
      expect(screen.queryByText("50%")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("group", { name: /probabilidade de vitória/i }),
      ).not.toBeInTheDocument();
      unmount();
    }
  });
});

/*
  Código que o contrato de hoje não declara — o que chegaria de um backend mais
  novo. O cast existe porque o tipo gerado do OpenAPI, por construção, não admite
  o valor: é a única forma de montar a resposta cujo tratamento se quer provar.
*/
const CODIGO_DESCONHECIDO = "fighter_suspended" as UnavailableCode;

describe("UnavailablePrediction — código ausente ou fora do enum", () => {
  it("cai num aviso genérico quando não veio código algum", () => {
    renderAviso(null);

    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByRole("heading")).toBeInTheDocument();
  });

  it("cai num aviso genérico para código que o contrato não declara", () => {
    // A tela degrada, não estoura, e o aviso nunca sai em branco.
    renderAviso(CODIGO_DESCONHECIDO);

    expect(screen.getByRole("heading").textContent).not.toBe("");
    expect(screen.getByText(PROSA)).toBeInTheDocument();
  });
});
