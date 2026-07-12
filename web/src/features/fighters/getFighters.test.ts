import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";
import { getFighters } from "@/features/fighters/getFighters";
import { server } from "@/mocks/server";

describe("getFighters", () => {
  it("desembrulha o envelope Page e devolve itens e total", async () => {
    const result = await getFighters();

    expect(result.total).toBe(3);
    expect(result.items).toHaveLength(3);
    expect(result.items[0]?.name).toBe("Jon Jones");
  });

  it("filtra server-side pelo parâmetro name", async () => {
    const result = await getFighters({ name: "volkanovski" });

    expect(result.items).toHaveLength(1);
    expect(result.items[0]?.name).toBe("Alexander Volkanovski");
  });

  it("lança ApiError com o status quando o backend responde com erro", async () => {
    server.use(
      http.get("*/api/v1/fighters", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );

    await expect(getFighters()).rejects.toBeInstanceOf(ApiError);
    await expect(getFighters()).rejects.toMatchObject({ status: 500 });
  });
});
