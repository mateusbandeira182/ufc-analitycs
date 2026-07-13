import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";
import { getEvents } from "@/features/events/getEvents";
import { server } from "@/mocks/server";

describe("getEvents", () => {
  it("desembrulha o envelope Page e devolve itens e total", async () => {
    const result = await getEvents();

    expect(result.total).toBe(3);
    expect(result.items).toHaveLength(3);
    expect(result.items[0]?.name).toBe("UFC 300");
  });

  it("pagina server-side por limit/offset e devolve a janela do envelope", async () => {
    const result = await getEvents({ limit: 2, offset: 2 });

    expect(result.total).toBe(3);
    expect(result.limit).toBe(2);
    expect(result.offset).toBe(2);
    expect(result.items).toHaveLength(1);
    expect(result.items[0]?.name).toMatch(/ribas vs\. namajunas/i);
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
