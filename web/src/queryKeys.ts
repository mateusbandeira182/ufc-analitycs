/*
  Query keys centralizadas e estáveis do TanStack Query.
  Fonte única para cache e invalidação previsíveis; as demais slices acrescentam
  suas chaves aqui (fighter, events, bout, head-to-head).
*/
export const queryKeys = {
  fighters: (params: { name: string }) => ["fighters", params] as const,
  fighter: (id: number) => ["fighter", id] as const,
  fighterBouts: (id: number) => ["fighter", id, "bouts"] as const,
  events: () => ["events"] as const,
  event: (id: number) => ["event", id] as const,
};
