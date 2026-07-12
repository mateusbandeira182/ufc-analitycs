import type { FighterOut } from "@/api/schema";

/*
  Fixtures de lutadores para os testes e handlers MSW.
  Espelham o shape real de FighterOut (contrato do M2), incluindo campos anuláveis.
*/
export const FIGHTER_FIXTURES: FighterOut[] = [
  {
    id: 1,
    name: "Jon Jones",
    nickname: "Bones",
    date_of_birth: "1987-07-19",
    height_cm: 193,
    reach_cm: 215,
    stance: "orthodox",
    wins: 27,
    losses: 1,
    draws: 0,
    source: "kaggle",
  },
  {
    id: 2,
    name: "Alexander Volkanovski",
    nickname: "The Great",
    date_of_birth: "1988-09-29",
    height_cm: 168,
    reach_cm: 182,
    stance: "orthodox",
    wins: 26,
    losses: 4,
    draws: 0,
    source: "kaggle",
  },
  {
    id: 3,
    name: "Israel Adesanya",
    nickname: "The Last Stylebender",
    date_of_birth: "1989-07-22",
    height_cm: 193,
    reach_cm: 203,
    stance: "switch",
    wins: 24,
    losses: 4,
    draws: 0,
    source: "kaggle",
  },
];
