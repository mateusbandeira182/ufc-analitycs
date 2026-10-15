import type {
  CommonOpponentOut,
  MatchupAttributeOut,
  MatchupBlockInfluenceOut,
  MatchupPredictionOut,
  UnavailableCode,
} from "@/api/schema";
import { FRESH_FEATURES } from "@/mocks/fighters";

/*
  Fixtures da explicabilidade do palpite (blocos de ablação, atributos as-of e os
  estados de indisponibilidade), espelhando o contrato do M9.
*/

/*
  Os onze blocos, na ordem de `BLOCK_REGISTRY` (`analysis/ablation.py`). As
  contagens de coluna saem do registro real: uma base as-of vira o trio
  `_a`/`_b`/`_diff`, uma feature bout-level vira coluna única.

  A distribuição de cobertura é a **medida** na Sprint 05, não a prevista: além
  dos três blocos que a SPEC antecipava sem cobertura (`divisao-categoria`,
  `divisao-taxa-base`, `confronto-de-bases`), `dinamica-por-round` saiu `none`
  num par real — 3 colunas no modelo, 0 avaliadas —, porque
  `round1_sig_strike_share_r3` depende do round-a-round, cujo backfill é parcial.
  É por isso que a tela não pode ter lista de blocos avaliáveis: a cobertura
  varia por par.

  `delta_prob_a` é `null` em TODO bloco `none` — jamais `0`. `null` quer dizer
  "não avaliado"; `0` quer dizer "medido e não deslocou", e `historico-qualificado`
  existe aqui justamente para fixar que os dois estados são distintos.

  As `description` reproduzem a prosa de engenharia que o backend manda de fato,
  com referência a documento interno e backtick de RST — a prova de que esse
  campo não é exibível a um fã de MMA.
*/
export const MATCHUP_BLOCK_FIXTURES: MatchupBlockInfluenceOut[] = [
  {
    name: "striking-profile",
    description: "Perfil de striking point-in-time do M5 (Slice 06).",
    n_columns: 18,
    n_columns_evaluated: 18,
    coverage: "full",
    delta_prob_a: 0.041,
  },
  {
    name: "dinamica-por-round",
    description: "Dinâmica por round point-in-time do M5 (Slice 06).",
    n_columns: 3,
    n_columns_evaluated: 0,
    coverage: "none",
    delta_prob_a: null,
  },
  {
    name: "dimensoes-ausentes",
    description:
      "Bloco A da SPEC 009 (Slice 01): precisão por dimensão, poder, ameaça de grappling e trabalho fora do golpe significativo -- 12 bases as-of, 36 colunas.",
    n_columns: 36,
    n_columns_evaluated: 36,
    coverage: "full",
    delta_prob_a: -0.062,
  },
  {
    name: "divisao-categoria",
    description:
      "Bloco B1a da SPEC 009 (Slice 02): a divisão como categoria física -- limite de peso real em libras mais o indicador de divisão feminina. Bout-level: coluna única, sem sufixo de canto.",
    n_columns: 2,
    n_columns_evaluated: 0,
    coverage: "none",
    delta_prob_a: null,
  },
  {
    name: "divisao-taxa-base",
    description:
      "Bloco B1b da SPEC 009 (Slice 02): a taxa de finalização histórica da divisão, point-in-time por data de evento estritamente anterior.",
    n_columns: 1,
    n_columns_evaluated: 0,
    coverage: "none",
    delta_prob_a: null,
  },
  {
    name: "formato",
    description:
      "Bloco B2 da SPEC 009 (Slice 03): luta de título, rounds agendados e experiência prévia em cinco rounds. ``scheduled_rounds`` é o formato AGENDADO, conhecido antes do gongo (RF-02).",
    n_columns: 5,
    n_columns_evaluated: 3,
    coverage: "partial",
    delta_prob_a: 0.008,
  },
  {
    name: "historico-qualificado",
    description:
      "Bloco C da SPEC 009 (Slice 04): como o lutador perde, quanta quilometragem acumulou e se está subindo ou caindo.",
    n_columns: 24,
    n_columns_evaluated: 24,
    coverage: "full",
    delta_prob_a: 0.0,
  },
  {
    name: "confronto-de-bases",
    description:
      "Bloco D1 da SPEC 009 (Slice 05): base contra base. Limitação declarada: ``fighters.stance`` é atributo lento NÃO versionado.",
    n_columns: 5,
    n_columns_evaluated: 0,
    coverage: "none",
    delta_prob_a: null,
  },
  {
    name: "eixos-de-estilo",
    description:
      "Bloco D2 da SPEC 009 (Slice 05): o vetor de estilo as-of de cada canto por fórmula fixa com divisores de domínio (sem PCA, sem fit aprendido).",
    n_columns: 7,
    n_columns_evaluated: 4,
    coverage: "partial",
    delta_prob_a: -0.013,
  },
  {
    name: "estilo-semelhante",
    description:
      "Bloco D3 da SPEC 009 (Slice 06): o histórico do lutador ponderado pela semelhança entre o adversário de cada luta anterior e o desta -- núcleo gaussiano que PONDERA e nunca filtra (RF-05).",
    n_columns: 3,
    n_columns_evaluated: 3,
    coverage: "full",
    delta_prob_a: 0.019,
  },
  {
    name: "forca-de-calendario",
    description:
      "Bloco D4 da SPEC 009 (Slice 06): a média do cartel as-of dos adversários anteriores. Adversário estreante fica fora do denominador (RF-03).",
    n_columns: 3,
    n_columns_evaluated: 3,
    coverage: "full",
    delta_prob_a: -0.004,
  },
];

/*
  Os oito atributos as-of **crus** (RF-19), nas chaves e rótulos que
  `MATCHUP_ATTRIBUTES` (`apps/predictions/services.py`) emite — a tela exibe o
  `label` do payload, nunca o redigita.

  Um nulo de cada lado de propósito: ausência de valor as-of é estado, e `0`
  seria uma taxa legítima afirmada sem medição. `finish_rate_prior` do lado B é
  zero **de verdade** (quem nunca finalizou tem taxa zero), e é o par que prova
  que a tela não confunde os dois.
*/
export const MATCHUP_ATTRIBUTE_FIXTURES: MatchupAttributeOut[] = [
  {
    key: "win_rate_prior",
    label: "Vitórias no histórico as-of",
    value_a: 0.9643,
    value_b: 0.8667,
  },
  {
    key: "finish_rate_prior",
    label: "Taxa de finalização",
    value_a: 0.5185,
    value_b: 0,
  },
  {
    key: "sig_strike_accuracy_r3",
    label: "Precisão de golpes significativos",
    value_a: 0.5712,
    value_b: 0.4938,
  },
  {
    key: "sig_strikes_landed_pm_r3",
    label: "Golpes significativos conectados por minuto",
    value_a: 4.312,
    value_b: 5.874,
  },
  {
    key: "sig_strikes_absorbed_pm_r3",
    label: "Golpes significativos absorvidos por minuto",
    value_a: 2.105,
    value_b: 3.449,
  },
  {
    key: "takedown_accuracy_r3",
    label: "Precisão de quedas",
    value_a: null,
    value_b: 0.3125,
  },
  {
    key: "takedown_defense_r3",
    label: "Defesa de quedas",
    value_a: 0.95,
    value_b: null,
  },
  {
    key: "control_time_avg_r3",
    label: "Tempo de controle médio por luta (segundos)",
    value_a: 214.5,
    value_b: 88.25,
  },
];

/*
  Prosa de indisponibilidade por código, verbatim das três mensagens que o
  backend produz (`apps/predictions/services.py`), com os ids embutidos. É texto
  para gente: a tela a exibe, nunca a interpreta — quem escolhe o aviso é o
  **código** (D-08).
*/
export const UNAVAILABLE_REASONS: Record<UnavailableCode, string> = {
  missing_history:
    "Sem histórico de lutas no granular para o(s) lutador(es) de id 2; sem lutas passadas não há features as-of para predizer.",
  missing_corners:
    "Luta sem os dois cantos cadastrados (é preciso exatamente um lutador no canto vermelho e um no azul).",
  repeated_fighter:
    "Lutador(es) de id 2 escalado(s) em mais de uma luta deste card; a predição exige um lutador por luta.",
};

/**
 * Palpite indisponível pelo código dado. As três probabilidades vêm nulas, e
 * `blocks`/`attributes` vêm **vazios** — o que o contrato chama de "não foi
 * produzido" (DP-05e), nunca listas de nulos, que diriam "foi medido e deu
 * ausente".
 */
export function unavailableMatchup(
  code: UnavailableCode,
  freshness = FRESH_FEATURES,
): MatchupPredictionOut {
  return {
    fighter_a: { id: 1, name: "Jon Jones" },
    fighter_b: { id: 2, name: "Alexander Volkanovski" },
    prob_a_wins: null,
    prob_b_wins: null,
    predicted_winner_id: null,
    unavailable_reason: UNAVAILABLE_REASONS[code],
    unavailable_code: code,
    blocks: [],
    attributes: [],
    features_freshness: freshness,
  };
}

/*
  Adversários em comum do par (1, 3) — Jon Jones e Israel Adesanya, que nunca se
  enfrentaram: os dois passaram por Alexander Volkanovski (id 2), com desfechos
  diferentes de cada lado. Adesanya aparece com DUAS lutas contra ele, uma
  perdida e uma sem vencedor, porque `winner_id` nulo é empate/no contest e não
  pode colapsar em derrota.

  O par (1, 2) fica de fora: eles se enfrentaram, e o próprio par nunca aparece
  em `common_opponents`. Assim cada seção da tela tem cenário próprio.
*/
export const JONES_ADESANYA_COMMON: CommonOpponentOut[] = [
  {
    opponent: { id: 2, name: "Alexander Volkanovski" },
    bouts_a: [
      {
        bout_id: 500,
        event_id: 42,
        event_name: "UFC 200",
        event_date: "2016-07-09",
        winner_id: 1,
        method: "ko_tko",
        round: 2,
      },
    ],
    bouts_b: [
      {
        bout_id: 610,
        event_id: 51,
        event_name: "UFC 243",
        event_date: "2019-10-06",
        winner_id: 2,
        method: "decision",
        round: 5,
      },
      {
        bout_id: 611,
        event_id: 60,
        event_name: "UFC 281",
        event_date: "2022-11-12",
        winner_id: null,
        method: "no_contest",
        round: 1,
      },
    ],
  },
];
