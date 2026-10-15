import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";
import { getEvents } from "@/features/events/getEvents";
import {
  EVENT_FIXTURES,
  PAST_EVENT_FIXTURES,
  UPCOMING_EVENT_FIXTURES,
} from "@/mocks/events";
import { server } from "@/mocks/server";

describe("getEvents", () => {
  it("desembrulha o envelope Page e devolve itens e total", async () => {
    const result = await getEvents();

    expect(result.total).toBe(EVENT_FIXTURES.length);
    expect(result.items[0]?.name).toBe("UFC 300");
  });

  it("pagina server-side por limit/offset e devolve a janela do envelope", async () => {
    const result = await getEvents({ limit: 2, offset: 2 });

    expect(result.total).toBe(EVENT_FIXTURES.length);
    expect(result.limit).toBe(2);
    expect(result.offset).toBe(2);
    expect(result.items).toHaveLength(2);
    expect(result.items[0]?.name).toMatch(/ribas vs\. namajunas/i);
  });

  it("filtra por `when=past` e devolve o total do conjunto FILTRADO", async () => {
    const result = await getEvents({ when: "past", limit: 50, offset: 0 });

    // O `total` reflete a partição, não o acervo — um total do acervo inteiro
    // faria a paginação da seção mentir enquanto os itens pareceriam certos.
    expect(result.total).toBe(PAST_EVENT_FIXTURES.length);
    expect(result.total).toBeLessThan(EVENT_FIXTURES.length);
  });

  it("filtra por `when=upcoming` devolvendo uma lista diferente da de `past`", async () => {
    const proximos = await getEvents({
      when: "upcoming",
      limit: 50,
      offset: 0,
    });
    const realizados = await getEvents({ when: "past", limit: 50, offset: 0 });

    expect(proximos.total).toBe(UPCOMING_EVENT_FIXTURES.length);
    expect(proximos.items.map((e) => e.id)).not.toEqual(
      realizados.items.map((e) => e.id),
    );
    // As duas partições são disjuntas e somam o acervo (CA-11).
    const ids = new Set([
      ...proximos.items.map((e) => e.id),
      ...realizados.items.map((e) => e.id),
    ]);
    expect(ids.size).toBe(proximos.total + realizados.total);
    expect(ids.size).toBe(EVENT_FIXTURES.length);
  });

  it("lança ApiError com o status quando o backend responde com erro", async () => {
    server.use(
      http.get("*/api/v1/events", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    await expect(getEvents()).rejects.toBeInstanceOf(ApiError);
    await expect(getEvents()).rejects.toMatchObject({ status: 500 });
  });
});
