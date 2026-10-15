import { describe, expect, it } from "vitest";

import {
  EVENTS_SECTION_PAGE_SIZE,
  hrefForSectionPage,
  offsetForSectionPage,
  parseSectionPage,
} from "@/features/events/sectionPages";

describe("parseSectionPage", () => {
  it("cai na página 1 quando o parâmetro está ausente", () => {
    expect(parseSectionPage(new URLSearchParams(), "realizados")).toBe(1);
  });

  it("lê a página 1-based quando o valor é um inteiro válido", () => {
    const params = new URLSearchParams("realizados=3&proximos=2");

    expect(parseSectionPage(params, "realizados")).toBe(3);
    expect(parseSectionPage(params, "proximos")).toBe(2);
  });

  it.each(["", "  ", "abc", "0", "-5", "2.5", "Infinity"])(
    "cai na página 1 com o valor inválido %j",
    (raw) => {
      const params = new URLSearchParams();
      params.set("realizados", raw);

      expect(parseSectionPage(params, "realizados")).toBe(1);
    },
  );
});

describe("offsetForSectionPage", () => {
  it("converte a página 1-based no offset do backend", () => {
    expect(offsetForSectionPage(1)).toBe(0);
    expect(offsetForSectionPage(3)).toBe(2 * EVENTS_SECTION_PAGE_SIZE);
  });
});

describe("hrefForSectionPage", () => {
  it("reescreve só o parâmetro da seção, preservando o da outra", () => {
    const params = new URLSearchParams("realizados=3&proximos=2");

    const href = hrefForSectionPage("/events", params, "realizados", 4);

    const query = new URLSearchParams(href.split("?")[1]);
    expect(query.get("realizados")).toBe("4");
    expect(query.get("proximos")).toBe("2");
  });

  it("remove o parâmetro na página 1, deixando a URL limpa", () => {
    const params = new URLSearchParams("realizados=3&proximos=2");

    const href = hrefForSectionPage("/events", params, "realizados", 1);

    const query = new URLSearchParams(href.split("?")[1]);
    expect(query.has("realizados")).toBe(false);
    expect(query.get("proximos")).toBe("2");
  });

  it("devolve só o pathname quando não resta nenhum parâmetro", () => {
    const params = new URLSearchParams("realizados=3");

    expect(hrefForSectionPage("/events", params, "realizados", 1)).toBe(
      "/events",
    );
  });
});
