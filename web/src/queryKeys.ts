/*
  Query keys centralizadas e estáveis do TanStack Query.
  Fonte única para cache e invalidação previsíveis; as demais slices acrescentam
  suas chaves aqui (fighter, events, bout, head-to-head).
*/
export const queryKeys = {
  fighters: (params: { name: string; limit: number; offset: number }) =>
    ["fighters", params] as const,
  fighter: (id: number) => ["fighter", id] as const,
  fighterBouts: (id: number) => ["fighter", id, "bouts"] as const,
  events: (params: { limit: number; offset: number }) =>
    ["events", params] as const,
  event: (id: number) => ["event", id] as const,
  bout: (id: number) => ["bout", id] as const,
  headToHead: (a: number | null, b: number | null) =>
    ["head-to-head", a, b] as const,
};
