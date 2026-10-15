import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { useEventPrediction } from "@/features/events/useEventPrediction";
import { EVENT_PREDICTION_FIXTURES } from "@/mocks/eventPrediction";
import { server } from "@/mocks/server";
import { queryKeys } from "@/queryKeys";

function wrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(
      QueryClientProvider,
      { client: queryClient },
      children,
    );
  };
}

describe("useEventPrediction — a requisição que não pode disparar", () => {
  it("não dispara a query quando o card não tem luta agendada", () => {
    /*
      `GET /predict/event/{id}` ESCREVE em `bout_predictions` com `source="api"`
      e commita, e o destaque da maior zebra lê essa tabela. Disparar num card
      realizado reescreveria um destaque histórico a cada render — por isso o
      `enabled` é condição de integridade de dado, não otimização de rede.
    */
    const { result } = renderHook(() => useEventPrediction(42, false), {
      wrapper: wrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(result.current.data).toBeUndefined();
  });

  it("não faz nenhuma chamada HTTP enquanto está desabilitado", async () => {
    let chamadas = 0;
    server.use(
      http.get("*/api/v1/predict/event/:id", () => {
        chamadas += 1;
        return HttpResponse.json(EVENT_PREDICTION_FIXTURES[42]);
      }),
    );

    renderHook(() => useEventPrediction(42, false), { wrapper: wrapper() });
    await waitFor(() => {
      expect(chamadas).toBe(0);
    });
  });

  it("busca o card previsto tipado quando há luta agendada", async () => {
    const { result } = renderHook(() => useEventPrediction(812, true), {
      wrapper: wrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data?.event_id).toBe(812);
    expect(result.current.data?.bouts).toHaveLength(3);
    // Indexação por `bout_id`: nome não é chave.
    expect(result.current.data?.bouts[2]?.bout_id).toBe(822);
  });

  it("não dispara com id de rota inválido mesmo habilitado", () => {
    // `/events/abc` -> `Number("abc")` é `NaN`: a página trata como não
    // encontrado, e a requisição não pode sair com `/predict/event/NaN`.
    const { result } = renderHook(() => useEventPrediction(Number.NaN, true), {
      wrapper: wrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
  });
});

describe("useEventPrediction — chave de cache", () => {
  it("usa uma entrada própria, separada da do detalhe do evento", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    function Wrapper({ children }: { children: ReactNode }) {
      return createElement(
        QueryClientProvider,
        { client: queryClient },
        children,
      );
    }

    const { result } = renderHook(() => useEventPrediction(812, true), {
      wrapper: Wrapper,
    });
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(
      queryClient.getQueryData(queryKeys.eventPrediction(812)),
    ).toBeDefined();
    // A entrada do detalhe do evento permanece intocada: duas requisições, dois
    // caches. Compartilhar a chave faria uma sobrescrever a outra.
    expect(queryClient.getQueryData(queryKeys.event(812))).toBeUndefined();
  });
});
