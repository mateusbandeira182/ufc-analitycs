import type { BoutMethod } from "@/api/schema";

/*
  Formatadores de exibição compartilhados entre features (fighters, events).
  Promovidos para cá quando uma segunda feature passou a precisar deles — a
  fonte única evita acoplamento entre features. Formatadores específicos de uma
  feature (ex.: cartel do lutador) permanecem na própria feature.
*/

/** Traço padrão para valores ausentes (dado anulável do contrato). */
export const DASH = "—";

const METHOD_LABELS: Record<BoutMethod, string> = {
  ko_tko: "KO/TKO",
  submission: "Finalização",
  decision: "Decisão",
  dq: "Desqualificação",
  no_contest: "Sem resultado",
};

/** Rótulo em pt-BR do método de encerramento da luta. */
export function formatMethod(method: BoutMethod): string {
  return METHOD_LABELS[method];
}

/**
 * Momento do encerramento como "R{round} {m:ss}" (ex.: "R2 4:15"). Traço quando
 * o round ou o tempo não vieram (ex.: decisão sem tempo de parada registrado).
 */
export function formatEndingTime(
  round: number | null,
  seconds: number | null,
): string {
  if (round === null || seconds === null) {
    return DASH;
  }
  const minutes = Math.floor(seconds / 60);
  const remaining = seconds % 60;
  return `R${String(round)} ${String(minutes)}:${String(remaining).padStart(2, "0")}`;
}

/**
 * Formata uma data ISO `YYYY-MM-DD` como `DD/MM/YYYY` em pt-BR. Formata a partir
 * dos componentes da string (não via `new Date`) para evitar deslize de fuso —
 * `new Date('2016-07-09')` é meia-noite UTC e exibiria o dia anterior em fusos
 * negativos. Traço quando a data é nula.
 */
export function formatIsoDate(iso: string | null): string {
  if (!iso) {
    return DASH;
  }
  const [year, month, day] = iso.split("-");
  return `${day}/${month}/${year}`;
}
