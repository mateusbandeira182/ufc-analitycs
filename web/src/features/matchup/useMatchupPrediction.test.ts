import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { useMatchupPrediction } from "@/features/matchup/useMatchupPrediction";

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

describe("useMatchupPrediction", () => {
  it("não dispara a query enquanto falta um dos lutadores", () => {
    const { result } = renderHook(() => useMatchupPrediction(1, null), {
      wrapper: wrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(result.current.data).toBeUndefined();
  });

  it("busca o palpite tipado quando os dois ids estão definidos", async () => {
    const { result } = renderHook(() => useMatchupPrediction(1, 2), {
      wrapper: wrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data?.fighter_a.id).toBe(1);
    expect(result.current.data?.fighter_b.id).toBe(2);
    expect(result.current.data?.predicted_winner_id).toBe(1);
  });
});
