import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MatchupBlockInfluenceOut } from "@/api/schema";
import { BlockInfluenceBars } from "@/features/confronto/BlockInfluenceBars";
import { MATCHUP_BLOCK_FIXTURES } from "@/mocks/matchup";

const NAME_A = "Jon Jones";
const NAME_B = "Alexander Volkanovski";

function renderBars(blocks = MATCHUP_BLOCK_FIXTURES) {
  return render(
    <BlockInfluenceBars blocks={blocks} nameA={NAME_A} nameB={NAME_B} />,
  );
}

/** A linha de um bloco, pelo rótulo acessível — escopo das asserções negativas. */
function row(label: RegExp): HTMLElement {
  return screen.getByRole("listitem", { name: label });
}

describe("BlockInfluenceBars — bloco sem medição", () => {
  it("rotula o bloco de cobertura nula como 'não avaliado'", () => {
    renderBars();

    expect(
      within(row(/divisão \(categoria de peso\)/i)).getByText(/não avaliado/i),
    ).toBeInTheDocument();
  });

  it("não exibe deslocamento algum na linha do bloco sem medição", () => {
    /*
      O modo de falha mais caro da sprint: tratar `delta_prob_a: null` como zero
      desenharia "0,0 p.p." onde não houve medição — um número plausível e errado,
      indistinguível de um certo a olho nu. A asserção é NEGATIVA e escopada a UM
      bloco por vez: uma varredura de tela acertaria por acidente, porque outros
      blocos legitimamente exibem "p.p.".
    */
    renderBars();

    for (const nome of [
      /dinâmica por round/i,
      /divisão \(categoria de peso\)/i,
      /divisão \(taxa de finalização\)/i,
      /confronto de bases/i,
    ]) {
      const linha = row(nome);
      expect(within(linha).queryByText(/p\.p\./)).not.toBeInTheDocument();
      expect(within(linha).queryByText(/favorece/i)).not.toBeInTheDocument();
      expect(
        within(linha).queryByText(/sem deslocamento/i),
      ).not.toBeInTheDocument();
    }
  });

  it("não desenha barra alguma para o bloco sem medição", () => {
    /*
      Largura zero ainda é uma barra: desenhá-la afirmaria visualmente uma
      medição inexistente. A barra é `aria-hidden` por construção (o texto
      carrega a informação), então nenhuma query semântica a alcança — daí o
      atributo de dado, que existe só para esta prova.
    */
    renderBars();

    expect(
      row(/divisão \(categoria de peso\)/i).querySelector(
        "[data-influence-bar]",
      ),
    ).toBeNull();
  });
});

describe("BlockInfluenceBars — zero medido não é zero ausente", () => {
  it("exibe o zero MEDIDO como deslocamento, não como ausência de medição", () => {
    /*
      `historico-qualificado` tem cobertura total e deslocamento 0,0: o modelo
      mediu e o bloco não deslocou. É o par que separa os dois estados — se a
      tela os colapsar, este teste e o anterior não podem passar juntos.
    */
    renderBars();
    const linha = row(/histórico qualificado/i);

    expect(within(linha).getByText("0,0 p.p.")).toBeInTheDocument();
    expect(within(linha).getByText(/sem deslocamento/i)).toBeInTheDocument();
    expect(within(linha).queryByText(/não avaliado/i)).not.toBeInTheDocument();
  });
});

describe("BlockInfluenceBars — o sinal escolhe de quem é o nome", () => {
  it("deslocamento positivo favorece o lado A", () => {
    renderBars();
    const linha = row(/perfil de striking/i);

    expect(within(linha).getByText("+4,1 p.p.")).toBeInTheDocument();
    expect(within(linha).getByText(`favorece ${NAME_A}`)).toBeInTheDocument();
  });

  it("deslocamento negativo favorece o lado B", () => {
    renderBars();
    const linha = row(/dimensões ausentes/i);

    expect(within(linha).getByText("-6,2 p.p.")).toBeInTheDocument();
    expect(within(linha).getByText(`favorece ${NAME_B}`)).toBeInTheDocument();
  });

  it("nomeia os dois lados do eixo, para a direção da barra significar algo", () => {
    // Sem a legenda dos cantos, "a barra foi para a direita" não diz nada.
    renderBars();
    const secao = screen.getByRole("region", { name: /moveu a agulha/i });

    // A seta faz parte da legenda do eixo: é ela que diz qual lado é qual, e é o
    // que distingue a legenda das linhas "favorece <nome>".
    expect(within(secao).getByText(`${NAME_A} →`)).toBeInTheDocument();
    expect(within(secao).getByText(`← ${NAME_B}`)).toBeInTheDocument();
  });
});

describe("BlockInfluenceBars — ordem, bloco desconhecido e lista vazia", () => {
  it("ordena por magnitude e joga os blocos sem medição para o fim", () => {
    renderBars();

    const nomes = screen
      .getAllByRole("listitem")
      .map((item) => item.getAttribute("aria-label"));

    expect(nomes).toEqual([
      "Dimensões ausentes",
      "Perfil de striking",
      "Desempenho contra estilo semelhante",
      "Eixos de estilo",
      "Formato da luta",
      "Força de calendário",
      "Histórico qualificado",
      "Dinâmica por round",
      "Divisão (categoria de peso)",
      "Divisão (taxa de finalização)",
      "Confronto de bases",
    ]);
  });

  it("renderiza bloco de nome desconhecido com rótulo degradado, nunca o esconde", () => {
    /*
      O mapa de rótulo decide só rótulo. Se decidisse presença, um bloco novo do
      backend desapareceria da tela em silêncio — a décima primeira falha muda
      desta SPEC.
    */
    const desconhecido: MatchupBlockInfluenceOut = {
      name: "bloco-novo-do-backend",
      description: "prosa de engenharia",
      n_columns: 4,
      n_columns_evaluated: 4,
      coverage: "full",
      delta_prob_a: 0.1,
    };
    renderBars([...MATCHUP_BLOCK_FIXTURES, desconhecido]);

    const linha = row(/bloco novo do backend/i);
    expect(within(linha).getByText("+10,0 p.p.")).toBeInTheDocument();
  });

  it("não renderiza a seção quando a resposta não trouxe bloco nenhum", () => {
    renderBars([]);

    expect(
      screen.queryByRole("region", { name: /moveu a agulha/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });

  it("não exibe a prosa de engenharia que o contrato manda em `description`", () => {
    /*
      `description` é prosa de engenharia com referência a documento interno
      ("Bloco B1a da SPEC 009 (Slice 02)...") e backtick de RST literal em 3 das
      11 entradas. Não significa nada para um fã de MMA: o rótulo em pt-BR é o
      que a tela exibe.
    */
    renderBars();

    expect(screen.queryByText(/SPEC 009/)).not.toBeInTheDocument();
    expect(screen.queryByText(/``/)).not.toBeInTheDocument();
  });
});
