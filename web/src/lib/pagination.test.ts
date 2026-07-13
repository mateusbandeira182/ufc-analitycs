import { describe, expect, it } from "vitest";

import {
  DEFAULT_PAGE_SIZE,
  getPageInfo,
  parsePaginationParams,
} from "@/lib/pagination";

describe("parsePaginationParams", () => {
  it("usa o tamanho de página padrão e offset zero quando ausentes", () => {
    const result = parsePaginationParams(new URLSearchParams());

    expect(result).toEqual({ limit: DEFAULT_PAGE_SIZE, offset: 0 });
  });

  it("lê limit e offset válidos da URL", () => {
    const result = parsePaginationParams(
      new URLSearchParams("limit=10&offset=20"),
    );

    expect(result).toEqual({ limit: 10, offset: 20 });
  });

  it("cai no padrão diante de valores inválidos (não numéricos, zero ou negativos)", () => {
    expect(parsePaginationParams(new URLSearchParams("limit=abc"))).toEqual({
      limit: DEFAULT_PAGE_SIZE,
      offset: 0,
    });
    expect(
      parsePaginationParams(new URLSearchParams("limit=0&offset=-5")),
    ).toEqual({ limit: DEFAULT_PAGE_SIZE, offset: 0 });
  });
});

describe("getPageInfo", () => {
  it("calcula a página atual e o total de páginas a partir do envelope", () => {
    const info = getPageInfo({ total: 50, limit: 10, offset: 20 });

    expect(info.currentPage).toBe(3);
    expect(info.totalPages).toBe(5);
    expect(info.isFirst).toBe(false);
    expect(info.isLast).toBe(false);
  });

  it("marca a primeira página (offset zero)", () => {
    const info = getPageInfo({ total: 50, limit: 10, offset: 0 });

    expect(info.currentPage).toBe(1);
    expect(info.isFirst).toBe(true);
    expect(info.isLast).toBe(false);
  });

  it("marca a última página", () => {
    const info = getPageInfo({ total: 50, limit: 10, offset: 40 });

    expect(info.currentPage).toBe(5);
    expect(info.isLast).toBe(true);
  });

  it("trata total zero como uma única página vazia", () => {
    const info = getPageInfo({ total: 0, limit: 10, offset: 0 });

    expect(info.totalPages).toBe(1);
    expect(info.currentPage).toBe(1);
    expect(info.isFirst).toBe(true);
    expect(info.isLast).toBe(true);
    expect(info.isOutOfRange).toBe(false);
  });

  it("sinaliza página fora do intervalo quando o offset ultrapassa o total", () => {
    const info = getPageInfo({ total: 30, limit: 10, offset: 40 });

    expect(info.isOutOfRange).toBe(true);
  });
});
