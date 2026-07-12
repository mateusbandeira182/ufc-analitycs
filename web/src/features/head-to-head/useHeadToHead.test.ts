import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { useHeadToHead } from "@/features/head-to-head/useHeadToHead";

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

describe("useHeadToHead", () => {
  it("não dispara a query quando falta um dos lutadores", () => {
    const { result } = renderHook(() => useHeadToHead(1, null), {
      wrapper: wrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(result.current.data).toBeUndefined();
  });

  it("não dispara a query quando os dois lutadores são iguais", () => {
    const { result } = renderHook(() => useHeadToHead(1, 1), {
      wrapper: wrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(result.current.data).toBeUndefined();
  });

  it("busca o confronto tipado quando os dois são distintos", async () => {
    const { result } = renderHook(() => useHeadToHead(1, 2), {
      wrapper: wrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data?.fighter_a_id).toBe(1);
    expect(result.current.data?.fighter_b_id).toBe(2);
    expect(result.current.data?.bouts).toHaveLength(1);
  });
});
