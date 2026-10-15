import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";
import { getLatestEvent } from "@/features/events/getLatestEvent";
import { server } from "@/mocks/server";

describe("getLatestEvent", () => {
  it("devolve o detalhe do último evento realizado, com resumo e destaques", async () => {
    // É **este** teste que prova a precedência de `/events/latest` sobre
    // `/events/:id`: ele corre sem override, contra os handlers base. Se a ordem
    // base estivesse invertida, o `:id` casaria o literal "latest",
    // `Number("latest")` seria `NaN`, a fixture viria `undefined` e a resposta
    // seria 404 em vez de "UFC 300" — é assim que a armadilha falha, calando.
    // Não apague este teste por parecer um caminho feliz trivial: ele é a única
    // cobertura da ordenação dos handlers.
    const event = await getLatestEvent();

    expect(event.name).toBe("UFC 300");
    expect(event.summary?.n_bouts).toBe(3);
    expect(event.highlights?.fastest_finish?.fighter.name).toBe("Bo Nickal");
  });

  it("propaga o 404 de acervo sem evento realizado como erro, não como corpo nulo", async () => {
    // Cobre o **caminho de 404**, não a ordem dos handlers: `server.use()`
    // prepende um handler de runtime, que tem prioridade sobre os do
    // `setupServer`, então este override interceptaria a chamada mesmo com a
    // ordem base errada. A precedência da rota é provada pelo primeiro teste.
    server.use(
      http.get("*/api/v1/events/latest", () =>
        HttpResponse.json(
          { detail: "Nenhum event realizado no acervo" },
          {
            status: 404,
          },
        ),
      ),
    );

    await expect(getLatestEvent()).rejects.toMatchObject({ status: 404 });
  });

  it("lança ApiError com o status quando o acervo não tem evento realizado", async () => {
    server.use(
      http.get("*/api/v1/events/latest", () =>
        HttpResponse.json({ detail: "boom" }, { status: 404 }),
      ),
    );

    await expect(getLatestEvent()).rejects.toBeInstanceOf(ApiError);
  });
});
