import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  FeaturesFreshnessOut,
  FighterOut,
  MatchupPredictionOut,
} from "@/api/schema";
import { MATERIALIZE_ASOF_COMMAND } from "@/components/StaleFeaturesBadge";
import { PredictionSection } from "@/features/confronto/PredictionSection";
import {
  FIGHTER_FIXTURES,
  FRESH_FEATURES,
  NEVER_MATERIALIZED_FEATURES,
  STALE_FEATURES,
} from "@/mocks/fighters";
import { unavailableMatchup } from "@/mocks/matchup";

/** Lutador da fixture pelo id; estourar aqui é falha de fixture, não do teste. */
function fighterFixture(id: number): FighterOut {
  const found = FIGHTER_FIXTURES.find((fighter) => fighter.id === id);
  if (!found) {
    throw new Error(`Fixture sem lutador de id ${String(id)}.`);
  }
  return found;
}

const FIGHTER_A = fighterFixture(1);
const FIGHTER_B = fighterFixture(2);

/** Palpite do caminho feliz: 27/53 -> 51% para o lado A, 49% para o B. */
function prediction(
  freshness: FeaturesFreshnessOut = FRESH_FEATURES,
): MatchupPredictionOut {
  const probA = 27 / 53;
  return {
    fighter_a: { id: FIGHTER_A.id, name: FIGHTER_A.name },
    fighter_b: { id: FIGHTER_B.id, name: FIGHTER_B.name },
    prob_a_wins: probA,
    prob_b_wins: 1 - probA,
    predicted_winner_id: FIGHTER_A.id,
    unavailable_reason: null,
    unavailable_code: null,
    blocks: [],
    attributes: [],
    features_freshness: freshness,
  };
}

function renderPrevisao(value: MatchupPredictionOut) {
  return render(
    <PredictionSection
      prediction={value}
      fighterA={FIGHTER_A}
      fighterB={FIGHTER_B}
    />,
  );
}

describe("PredictionSection — palpite disponível", () => {
  it("destaca o vencedor previsto e mostra as duas probabilidades complementares", () => {
    renderPrevisao(prediction());

    const vencedor = screen.getByRole("status", {
      name: /vencedor previsto/i,
    });
    expect(
      within(vencedor).getByRole("heading", { name: /jon jones/i }),
    ).toBeInTheDocument();

    const grupo = screen.getByRole("group", {
      name: /probabilidade de vitória/i,
    });
    expect(within(grupo).getByText("51%")).toBeInTheDocument();
    expect(within(grupo).getByText("49%")).toBeInTheDocument();
  });

  it("nomeia os dois lados do confronto", () => {
    renderPrevisao(prediction());
    const grupo = screen.getByRole("group", {
      name: /probabilidade de vitória/i,
    });

    expect(within(grupo).getByText(FIGHTER_A.name)).toBeInTheDocument();
    expect(within(grupo).getByText(FIGHTER_B.name)).toBeInTheDocument();
  });
});

describe("PredictionSection — palpite indisponível", () => {
  it("delega o aviso e não exibe porcentagem alguma na seção", () => {
    /*
      CA-15: para luta com estreante, a seção não renderiza barra nem
      porcentagem — renderiza o aviso. Nunca 50/50, que se leria como opinião do
      modelo quando é ausência dela.
    */
    renderPrevisao(unavailableMatchup("missing_history"));

    expect(
      screen.getByRole("heading", { name: /sem histórico para prever/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/\d+\s*%/)).not.toBeInTheDocument();
    expect(screen.queryByText("50%")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("group", { name: /probabilidade de vitória/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("status", { name: /vencedor previsto/i }),
    ).not.toBeInTheDocument();
  });

  it("trata probabilidade nula como ausência, mesmo sem código de indisponibilidade", () => {
    /*
      `clampProbability(null)` renderizaria "0%" — uma afirmação sobre a luta que
      ninguém mediu. Quem decide é o corpo da resposta, não só o código.
    */
    renderPrevisao({
      ...prediction(),
      prob_a_wins: null,
      prob_b_wins: null,
      predicted_winner_id: null,
    });

    expect(screen.queryByText("0%")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("group", { name: /probabilidade de vitória/i }),
    ).not.toBeInTheDocument();
  });
});

describe("PredictionSection — aviso de dado defasado (D-10)", () => {
  it("reusa o badge da Sprint 06, com o comando da constante exportada", () => {
    // Asserido pela constante, não por string literal: um segundo badge, ou um
    // comando redigitado, falha aqui.
    renderPrevisao(prediction(STALE_FEATURES));

    expect(screen.getByText(MATERIALIZE_ASOF_COMMAND)).toBeInTheDocument();
  });

  it("não avisa nada quando o cache está em dia", () => {
    renderPrevisao(prediction(FRESH_FEATURES));

    expect(
      screen.queryByText(MATERIALIZE_ASOF_COMMAND),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/dado defasado/i)).not.toBeInTheDocument();
  });

  it("avisa TAMBÉM no estado indisponível, com o cache nunca materializado", () => {
    /*
      É o `features_freshness` que distingue "estreante" de "cache nunca
      materializado" — está escrito no docstring de `explain_matchup`. Esconder o
      aviso no ramo indisponível deixaria o usuário sem o discriminador das duas
      causas.
    */
    renderPrevisao(
      unavailableMatchup("missing_history", NEVER_MATERIALIZED_FEATURES),
    );

    expect(screen.getByText(MATERIALIZE_ASOF_COMMAND)).toBeInTheDocument();
    expect(screen.getByText(/nunca foi materializado/i)).toBeInTheDocument();
    // E continua sem porcentagem: o aviso de cache não reintroduz número.
    expect(screen.queryByText(/\d+\s*%/)).not.toBeInTheDocument();
  });
});
