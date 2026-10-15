import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { FeaturesFreshnessOut } from "@/api/schema";
import {
  MATERIALIZE_ASOF_COMMAND,
  StaleFeaturesBadge,
} from "@/components/StaleFeaturesBadge";

/*
  O aviso de defasagem do cache as-of. Nasce aqui (D-10) com a primeira
  consumidora real — a seção FORMA ATUAL da página do lutador — e é reusado,
  nunca duplicado, pelas Sprints 08 e 09.

  Duas armadilhas conhecidas moram nas asserções negativas deste arquivo: o
  comando do grão LUTA é prefixo do comando do grão LUTADOR, e `materialized_at`
  é um datetime que `formatIsoDate` destruiria em silêncio.
*/

function freshness(
  overrides: Partial<FeaturesFreshnessOut> = {},
): FeaturesFreshnessOut {
  return {
    materialized_at: "2026-09-01T00:00:00Z",
    n_bouts_source: 8000,
    n_bouts_current: 8120,
    stale: true,
    ...overrides,
  };
}

describe("StaleFeaturesBadge", () => {
  it("não renderiza nada quando o cache está em dia", () => {
    const { container } = render(
      <StaleFeaturesBadge freshness={freshness({ stale: false })} />,
    );

    // Aviso permanente é aviso ignorado: em dia, a seção não ganha ruído.
    expect(container).toBeEmptyDOMElement();
  });

  it("mostra a data da última materialização e o comando que corrige", () => {
    render(<StaleFeaturesBadge freshness={freshness()} />);

    const aviso = screen.getByRole("status");
    expect(aviso).toHaveTextContent("01/09/2026");
    expect(aviso).toHaveTextContent(MATERIALIZE_ASOF_COMMAND);
  });

  it("declara que o cache nunca foi materializado, sem data nem traço mudo", () => {
    render(
      <StaleFeaturesBadge freshness={freshness({ materialized_at: null })} />,
    );

    const aviso = screen.getByRole("status");
    expect(aviso).toHaveTextContent(/nunca foi materializado/i);
    // Ausência declarada, não data inventada nem um traço que não se explica.
    expect(aviso).not.toHaveTextContent("/2026");
    expect(aviso).not.toHaveTextContent("—");
    expect(aviso).toHaveTextContent(MATERIALIZE_ASOF_COMMAND);
  });

  it("aponta o estágio do cache por lutador, e não o do cache por luta", () => {
    // `--stage materialize` é PREFIXO de `--stage materialize-asof`. Apontar o
    // estágio errado manda o usuário reconstruir o cache por LUTA, e o defasado
    // continua defasado — falha que só uma asserção negativa acusa.
    expect(MATERIALIZE_ASOF_COMMAND.endsWith("--stage materialize")).toBe(
      false,
    );
    expect(MATERIALIZE_ASOF_COMMAND).toBe(
      "python -m ingestion.features build --stage materialize-asof",
    );
  });

  it("formata o datetime até o dia, sem vazar hora nem o 'T' do ISO", () => {
    render(
      <StaleFeaturesBadge
        freshness={freshness({ materialized_at: "2026-09-01T00:00:00Z" })}
      />,
    );

    // `formatIsoDate` faz `split("-")` e produziria "01T00:00:00Z/09/2026" sem
    // erro nenhum: o badge corta para dez caracteres antes de formatar.
    const aviso = screen.getByRole("status");
    expect(aviso).not.toHaveTextContent("00:00:00");
    expect(aviso).not.toHaveTextContent("01T");
    expect(aviso).toHaveTextContent("01/09/2026");
  });
});
