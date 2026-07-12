import type { BoutMethod, FighterOut, Stance } from "@/api/schema";

/** Traço padrão para valores ausentes (dado anulável do contrato). */
const DASH = "—";

/** Cartel no formato "V-D-E" (vitórias-derrotas-empates). */
export function formatRecord(fighter: FighterOut): string {
  return `${String(fighter.wins)}-${String(fighter.losses)}-${String(fighter.draws)}`;
}

const STANCE_LABELS: Record<Stance, string> = {
  orthodox: "Ortodoxa",
  southpaw: "Canhota",
  switch: "Alternada",
};

/** Rótulo em pt-BR da base (guarda) do lutador; traço quando desconhecida. */
export function formatStance(stance: Stance | null): string {
  return stance ? STANCE_LABELS[stance] : DASH;
}

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
 * Rótulo do resultado da luta pela ótica do lutador. `won` é booleano derivado
 * pelo backend; empate e no contest chegam como `won=false`. A linha só
 * distingue "Sem resultado" (no contest); o cartel V/D/E do topo é a fonte
 * autoritativa do retrospecto — a linha não infere empate (o dado não vem).
 */
export function formatResult(bout: {
  won: boolean;
  method: BoutMethod;
}): string {
  if (bout.won) {
    return "Vitória";
  }
  return bout.method === "no_contest" ? "Sem resultado" : "Derrota";
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

/** Altura em centímetros; traço quando ausente. */
export function formatHeight(heightCm: number | null): string {
  return heightCm === null ? DASH : `${String(heightCm)} cm`;
}

/** Alcance em centímetros; traço quando ausente. */
export function formatReach(reachCm: number | null): string {
  return reachCm === null ? DASH : `${String(reachCm)} cm`;
}
