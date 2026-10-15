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

  it("filtra server-side pelo parâmetro q, batendo no nome", async () => {
    const result = await getFighters({ q: "volkanovski" });

    expect(result.items).toHaveLength(1);
    expect(result.items[0]?.name).toBe("Alexander Volkanovski");
  });

  it("encontra pelo apelido, e não só pelo nome", async () => {
    // "Bones" não aparece no nome de nenhum lutador do acervo — só no apelido
    // de Jon Jones. É o comportamento que o parâmetro `q` passou a oferecer.
    const result = await getFighters({ q: "bones" });

    expect(result.items).toHaveLength(1);
    expect(result.items[0]?.name).toBe("Jon Jones");
  });

  it("manda o termo como `q` na query string, e nunca como `name`", async () => {
    // Callsite que ainda usasse `name` não quebraria: o FastAPI ignora query
    // param desconhecido e devolve o acervo inteiro — a busca simplesmente
    // deixaria de filtrar, em silêncio.
    let recebido: URL | null = null;
    server.use(
      http.get("*/api/v1/fighters", ({ request }) => {
        recebido = new URL(request.url);
        return HttpResponse.json({ items: [], total: 0, limit: 50, offset: 0 });
      }),
    );

    await getFighters({ q: "bones" });

    const url = recebido as unknown as URL;
    expect(url.searchParams.get("q")).toBe("bones");
    expect(url.searchParams.has("name")).toBe(false);
  });

  it("pagina server-side por limit/offset e devolve a janela do envelope", async () => {
    const result = await getFighters({ limit: 2, offset: 2 });

    expect(result.total).toBe(3);
    expect(result.limit).toBe(2);
    expect(result.offset).toBe(2);
    expect(result.items).toHaveLength(1);
    expect(result.items[0]?.name).toBe("Israel Adesanya");
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
