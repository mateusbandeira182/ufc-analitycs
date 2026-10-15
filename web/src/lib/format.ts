import type { BoutMethod } from "@/api/schema";

/*
  Formatadores de exibição compartilhados entre features (fighters, events).
  Promovidos para cá quando uma segunda feature passou a precisar deles — a
  fonte única evita acoplamento entre features. Formatadores específicos de uma
  feature (ex.: cartel do lutador) permanecem na própria feature.
*/

/** Traço padrão para valores ausentes (dado anulável do contrato). */
export const DASH = "—";

/**
 * Duração em segundos como `m:ss` (ex.: `315` -> `"5:15"`). Compartilhada entre a
 * página da luta (tempo de encerramento e de controle) e as estatísticas do
 * lutador (média de tempo de controle). Traço quando nula.
 */
export function formatDuration(totalSeconds: number | null): string {
  if (totalSeconds === null) {
    return DASH;
  }
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes)}:${String(seconds).padStart(2, "0")}`;
}

const METHOD_LABELS: Record<BoutMethod, string> = {
  ko_tko: "KO/TKO",
  submission: "Finalização",
  decision: "Decisão",
  dq: "Desqualificação",
  no_contest: "Sem resultado",
};

/**
 * Rótulo em pt-BR do método de encerramento da luta. `method` é nulável no
 * contrato desde que `bouts` passou a conter luta agendada: ausência de método
 * é ausência de desfecho, rotulada como tal — nem "Sem resultado" (que é o no
 * contest, um desfecho de verdade), nem um traço mudo.
 */
export function formatMethod(method: BoutMethod | null): string {
  return method === null ? "Agendada" : METHOD_LABELS[method];
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

/*
  Probabilidade do palpite (0..1) como percentual inteiro. Promovidos de
  `features/confronto/format.ts` quando o card de evento futuro passou a precisar
  deles — mesmo precedente de `formatDuration`. O backend garante que as duas
  probabilidades somam 1, mas a tela nunca confia cegamente: valor não finito ou
  fora de [0,1] é saneado antes de virar texto ou largura de barra.
*/

/** Fração saneada para [0,1]; não finito vira 0. Base comum de texto e barra. */
export function clampProbability(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.min(1, Math.max(0, value));
}

/** Fração (0..1) como percentual inteiro (ex.: `0.5094` -> `"51%"`). */
export function formatProbability(value: number): string {
  return `${String(Math.round(clampProbability(value) * 100))}%`;
}
