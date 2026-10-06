import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Avatar } from "@/components/ui/avatar";

/*
  Avatar do lutador. O retrato (`headshot_url`) existe para 794 dos 2.754
  lutadores do acervo, e lutador histórico nunca terá — o fallback por iniciais
  é o caso comum, não a exceção. Os testes travam o comportamento observável nos
  dois estados e o contrato de acessibilidade uniforme entre eles: em ambos, quem
  usa leitor de tela ouve o nome do lutador, nunca as iniciais soltas.
*/

describe("Avatar", () => {
  it("renderiza as iniciais quando o lutador não tem retrato", () => {
    render(<Avatar name="Jon Jones" src={null} />);

    expect(screen.getByText("JJ")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("não repete o nome do lutador como texto no fallback", () => {
    render(<Avatar name="Jon Jones" src={null} />);

    // O avatar é acompanhado do nome em toda tela que o usa hoje. Repetir o
    // nome dentro dele duplicaria o texto na linha da tabela.
    expect(screen.queryByText("Jon Jones")).not.toBeInTheDocument();
  });

  it("renderiza a imagem do retrato, acessível pelo nome do lutador", () => {
    render(
      <Avatar name="Jon Jones" src="https://imagens.ufc.test/jon-jones.png" />,
    );

    const retrato = screen.getByRole("img", { name: "Jon Jones" });
    expect(retrato).toHaveAttribute(
      "src",
      "https://imagens.ufc.test/jon-jones.png",
    );
    expect(screen.queryByText("JJ")).not.toBeInTheDocument();
  });

  it("cai no fallback quando o retrato é ausente na prop", () => {
    render(<Avatar name="Jon Jones" />);

    expect(screen.getByText("JJ")).toBeInTheDocument();
  });

  it("usa uma única inicial quando o nome tem uma palavra só", () => {
    render(<Avatar name="Shogun" src={null} />);

    expect(screen.getByText("S")).toBeInTheDocument();
  });

  it("preserva letra não-ASCII na inicial, sem normalizar o nome", () => {
    // A transliteração de `normalize_name` é assunto da ingestão (casamento de
    // entidade). A inicial exibida na tela é o nome como ele é: normalizar aqui
    // apagaria o `Ł` em vez de transliterá-lo.
    render(<Avatar name="Łukasz Brzeski" src={null} />);

    expect(screen.getByText("ŁB")).toBeInTheDocument();
  });

  it("usa a primeira e a última palavra em nome com mais de duas palavras", () => {
    render(<Avatar name="Anderson Silva Ferreira" src={null} />);

    expect(screen.getByText("AF")).toBeInTheDocument();
  });

  it("cai em interrogação quando o nome é vazio ou só espaços", () => {
    const { rerender } = render(<Avatar name="" src={null} />);
    expect(screen.getByText("?")).toBeInTheDocument();

    rerender(<Avatar name="   " src={null} />);
    expect(screen.getByText("?")).toBeInTheDocument();
  });

  it("aceita o tamanho pelo className, sem variante própria", () => {
    render(<Avatar name="Jon Jones" src={null} className="size-16" />);

    expect(screen.getByText("JJ").parentElement).toHaveClass("size-16");
  });
});
