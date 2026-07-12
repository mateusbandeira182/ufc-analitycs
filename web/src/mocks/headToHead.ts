import type { BoutDetailOut, BoutFighterStatsOut } from "@/api/schema";

/*
  Fixtures de confronto direto (head-to-head) para os testes e handlers MSW.
  Espelham o shape real de HeadToHeadOut/BoutDetailOut (contrato do M2): cada
  confronto traz o box-score granular dos dois cantos, nunca médias agregadas.
*/

/** Stats granulares de um canto, com valores plausíveis para asserção nos testes. */
function stats(
  fighterId: number,
  name: string,
  corner: "red" | "blue",
  overrides: Partial<BoutFighterStatsOut> = {},
): BoutFighterStatsOut {
  return {
    fighter_id: fighterId,
    name,
    corner,
    knockdowns: 1,
    sig_strikes_landed: 42,
    sig_strikes_attempted: 88,
    takedowns_landed: 1,
    takedowns_attempted: 3,
    submission_attempts: 0,
    control_time_seconds: 96,
    source: "kaggle",
    ...overrides,
  };
}

/*
  Confronto direto entre o lutador 1 (Jon Jones, vencedor) e o 2 (Alexander
  Volkanovski), no UFC 200. Um único bout — basta para exercitar a lista de
  confrontos, o vencedor derivado de `winner_id` e o link para /bouts/:id.
*/
const JONES_VS_VOLKANOVSKI: BoutDetailOut[] = [
  {
    id: 500,
    event: {
      id: 42,
      name: "UFC 200",
      date: "2016-07-09",
      location: "Las Vegas, USA",
      source: "kaggle",
    },
    winner_id: 1,
    method: "ko_tko",
    round: 2,
    ending_time_seconds: 84,
    weight_class: "Light Heavyweight",
    source: "kaggle",
    fighters: [
      stats(1, "Jon Jones", "red", { knockdowns: 1 }),
      stats(2, "Alexander Volkanovski", "blue", {
        knockdowns: 0,
        sig_strikes_landed: 25,
        control_time_seconds: 30,
      }),
    ],
  },
];

/** Chave canônica do par (independe da ordem a/b). */
function pairKey(a: number, b: number): string {
  return [a, b].sort((x, y) => x - y).join("-");
}

/*
  Confrontos por par de lutadores. Ausente no mapa significa "nunca se
  enfrentaram" (lista vazia com os dois existentes — 200, distinto do 404).
*/
const DIRECT_BOUTS: Record<string, BoutDetailOut[]> = {
  [pairKey(1, 2)]: JONES_VS_VOLKANOVSKI,
};

/** Confrontos diretos do par; lista vazia quando os dois nunca se enfrentaram. */
export function headToHeadBouts(a: number, b: number): BoutDetailOut[] {
  return DIRECT_BOUTS[pairKey(a, b)] ?? [];
}
