"""Testes da matriz de confronto (matchup) bout-level -- Plano 005-04 (CA-04).

Cobrem as funções puras de ``ingestion.features.matchup`` sobre DataFrame
sintético (Pandas puro, sem Postgres): o pivô da frame longa por lutador-luta de
volta para uma linha por bout (canto A = red, B = blue), os diferenciais A menos
B, a derivação do alvo ``winner_corner`` separada das features com exclusão de
no-contest/draw, e o baseline ingênuo (taxa de vitória do corner vermelho). O
teste do estágio do CLI monkeypatcha o builder da frame longa enriquecida upstream
-- não toca o banco -- e confirma que o baseline é emitido via ``logging``.

Reconciliação com o contrato real da Slice 01: a frame longa **não** carrega
``winner_id`` (ver ``LONG_FRAME_COLUMNS``); carrega ``result`` por canto
(win/loss/no_contest/draw). O alvo é derivado de ``result_a`` -- que já distingue
NC/draw -- em vez de comparar ``winner_id`` com ``fighter_id`` (snippet ilustrativo
do plano, escrito contra um contrato presumido).
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
import pytest
from pandas.errors import MergeError

from analysis.dataset import PROSCRIBED_FEATURE_BASES
from apps.bouts.enums import BoutMethod, Corner
from apps.fighters.enums import Stance
from ingestion.features.division import (
    DIVISION_CATEGORY_FEATURES,
    DIVISION_FINISH_RATE_PRIOR,
    IS_WOMENS_DIVISION,
    WEIGHT_CLASS_LBS,
)
from ingestion.features.long_frame import LONG_FRAME_COLUMNS
from ingestion.features.matchup import (
    _BOUT_CONTEXT_BASES,
    _NON_FEATURE_BASES,
    _OUTCOME_BASES,
    _SUFFIX_DIFF,
    BOUT_LEVEL_FEATURE_COLUMNS,
    COL_CORNER,
    COL_GRAPPLING_AXIS,
    COL_STANCE,
    COL_TITLE_BOUT,
    COL_VOLUME_AXIS,
    COL_WEIGHT_CLASS,
    FORMAT_FEATURES,
    IS_TITLE_BOUT,
    SCHEDULED_ROUNDS,
    TARGET_COLUMN,
    MatchupMatrix,
    add_differentials,
    build_matchup_matrix,
    collapse_bout_context_columns,
    derive_target,
    numeric_feature_bases,
    pivot_corners,
    red_corner_win_rate,
)
from ingestion.features.rolling import (
    BODY_ACCURACY_R3,
    CAREER_MINUTES_BEFORE,
    CLINCH_ACCURACY_R3,
    CONTROL_TIME_AVG_TREND,
    DISTANCE_ACCURACY_R3,
    FIVE_ROUND_BOUTS_BEFORE,
    GROUND_ACCURACY_R3,
    HEAD_ACCURACY_R3,
    KNOCKDOWNS_AVG_R3,
    KNOCKDOWNS_PM_R3,
    KO_LOSSES_PRIOR,
    LEG_ACCURACY_R3,
    OPPONENT_WIN_RATE_PRIOR_AVG,
    SIG_STRIKE_ACCURACY_R3,
    SIG_STRIKES_ABSORBED_PM_TREND,
    SIG_STRIKES_LANDED_PM_TREND,
    SIMILAR_STYLE_WIN_RATE_PRIOR,
    SUBMISSION_ATTEMPTS_AVG_R3,
    SUBMISSION_LOSSES_PRIOR,
    TAKEDOWN_ACCURACY_R3,
    TAKEDOWN_DEFENSE_TREND,
    TAKEDOWNS_LANDED_AVG_TREND,
    TOTAL_TO_SIG_STRIKE_RATIO_R3,
)

# Colunas cruas que o bloco A da SPEC 009 consome; todas já existiam na frame longa.
_BLOCO_A_COLUNAS_CRUAS: tuple[str, ...] = (
    "sig_strikes_landed",
    "sig_strikes_attempted",
    "head_landed",
    "head_attempted",
    "body_landed",
    "body_attempted",
    "leg_landed",
    "leg_attempted",
    "distance_landed",
    "distance_attempted",
    "clinch_landed",
    "clinch_attempted",
    "ground_landed",
    "ground_attempted",
    "takedowns_landed",
    "takedowns_attempted",
    "knockdowns",
    "submission_attempts",
    "total_strikes_landed",
)

# As 12 bases as-of derivadas pelo bloco A -- elas **têm** de virar feature.
_BLOCO_A_BASES: tuple[str, ...] = (
    SIG_STRIKE_ACCURACY_R3,
    HEAD_ACCURACY_R3,
    BODY_ACCURACY_R3,
    LEG_ACCURACY_R3,
    DISTANCE_ACCURACY_R3,
    CLINCH_ACCURACY_R3,
    GROUND_ACCURACY_R3,
    TAKEDOWN_ACCURACY_R3,
    KNOCKDOWNS_PM_R3,
    KNOCKDOWNS_AVG_R3,
    SUBMISSION_ATTEMPTS_AVG_R3,
    TOTAL_TO_SIG_STRIKE_RATIO_R3,
)

_FEATURE = "sig_strikes_pm_asof"

# Data única das fixtures sintéticas: o contexto temporal só importa nos testes de divisão.
_DATA_PADRAO = date(2023, 1, 1)

# Rótulo da linha de log dedicada às colunas bout-level no estágio ``matchup``.
_ROTULO_BOUT_LEVEL = "Features bout-level"


def _corner_row(
    bout_id: int,
    corner: Corner,
    fighter_id: int,
    result: str,
    feature: float,
    weight_class: str | None = "lightweight",
    title_bout: bool | None = False,
    scheduled_rounds: int | None = 3,
) -> dict[str, object]:
    """Uma linha lutador-luta mínima da frame longa (uma participação).

    O contexto de luta vem da mesma linha de ``bouts`` e por isso é **idêntico** nos dois
    cantos -- é exatamente o que torna o par ``_a``/``_b`` degenerado e exige o colapso.

    ``title_bout`` e ``scheduled_rounds`` viraram parâmetros na Slice 03 (bloco B2): as
    features de formato precisam de lutas de cinco rounds, de disputa de cinturão e de
    contexto **nulo** na mesma fixture, e um valor fixo não distinguiria nenhum dos três.

    ``stance`` e os dois eixos de estilo entram na Slice 05 (blocos D1/D2). Não são colunas
    cruas de ``LONG_FRAME_COLUMNS``: nascem depois, na trajetória e na forma recente. Estão
    aqui porque ``build_matchup_matrix`` recebe a frame longa **enriquecida**, e os testes
    deste arquivo que passam pelo orquestrador precisam honrar esse contrato -- valores
    constantes, porque o que eles medem é o pivô, não o confronto de bases nem o estilo.
    """
    return {
        "bout_id": bout_id,
        "corner": corner,
        "fighter_id": fighter_id,
        "result": result,
        "weight_class": weight_class,
        "title_bout": title_bout,
        "scheduled_rounds": scheduled_rounds,
        "event_date": _DATA_PADRAO,
        "method": BoutMethod.DECISION.value,
        COL_STANCE: Stance.ORTHODOX.value,
        COL_GRAPPLING_AXIS: 0.5,
        COL_VOLUME_AXIS: 0.5,
        _FEATURE: feature,
    }


def _bout_rows(
    bout_id: int,
    red_fighter_id: int,
    blue_fighter_id: int,
    red_result: str,
    red_feature: float,
    blue_feature: float,
    title_bout: bool | None = False,
    scheduled_rounds: int | None = 3,
) -> list[dict[str, object]]:
    """Duas linhas (red/blue) de um bout; o resultado do azul é o espelho do vermelho."""
    blue_result = {"win": "loss", "loss": "win"}.get(red_result, red_result)
    return [
        _corner_row(
            bout_id,
            Corner.RED,
            red_fighter_id,
            red_result,
            red_feature,
            title_bout=title_bout,
            scheduled_rounds=scheduled_rounds,
        ),
        _corner_row(
            bout_id,
            Corner.BLUE,
            blue_fighter_id,
            blue_result,
            blue_feature,
            title_bout=title_bout,
            scheduled_rounds=scheduled_rounds,
        ),
    ]


def test_pivot_um_por_bout() -> None:
    """CA-04.1: o pivô produz uma linha por bout, red em ``*_a`` e blue em ``*_b``."""
    long = pd.DataFrame(
        [
            *_bout_rows(
                1,
                red_fighter_id=10,
                blue_fighter_id=20,
                red_result="win",
                red_feature=4.0,
                blue_feature=3.0,
            ),
            *_bout_rows(
                2,
                red_fighter_id=30,
                blue_fighter_id=40,
                red_result="loss",
                red_feature=2.0,
                blue_feature=5.0,
            ),
        ]
    )

    matrix = pivot_corners(long)

    assert len(matrix) == 2
    row1 = matrix.loc[matrix["bout_id"] == 1].iloc[0]
    assert row1["fighter_id_a"] == 10
    assert row1["fighter_id_b"] == 20
    assert row1[f"{_FEATURE}_a"] == 4.0
    assert row1[f"{_FEATURE}_b"] == 3.0
    # A coluna ``corner`` é consumida pelo pivô -- não sobrevive como feature.
    assert "corner" not in matrix.columns
    assert "corner_a" not in matrix.columns


def test_pivot_bout_malformado_levanta() -> None:
    """CA-04.1: um bout com dois cantos vermelhos viola ``one_to_one`` e levanta."""
    long = pd.DataFrame(
        [
            _corner_row(1, Corner.RED, 10, "win", 4.0),
            _corner_row(1, Corner.RED, 11, "loss", 3.0),
            _corner_row(1, Corner.BLUE, 20, "loss", 2.0),
        ]
    )

    with pytest.raises(MergeError):
        pivot_corners(long)


def test_diferenciais_apenas_features_numericas() -> None:
    """CA-04.2: ``<feature>_diff == _a - _b``; identidade e categóricas sem ``*_diff``."""
    matrix = pd.DataFrame(
        [
            {
                "bout_id": 1,
                "fighter_id_a": 10,
                "fighter_id_b": 20,
                "stance_a": Stance.ORTHODOX,
                "stance_b": Stance.SOUTHPAW,
                f"{_FEATURE}_a": 4.0,
                f"{_FEATURE}_b": 3.0,
            }
        ]
    )

    bases = numeric_feature_bases(matrix)
    result = add_differentials(matrix, bases)

    assert bases == [_FEATURE]
    assert result[f"{_FEATURE}_diff"].iloc[0] == pytest.approx(1.0)
    # Identidade (numérica) e categóricas não geram diferencial.
    assert "fighter_id_diff" not in result.columns
    assert "stance_diff" not in result.columns


def test_alvo_e_exclusao_nc_draw() -> None:
    """CA-04.3: alvo R/B derivado, fora das features; NC/draw excluído e contado."""
    long = pd.DataFrame(
        [
            *_bout_rows(1, 10, 20, red_result="win", red_feature=4.0, blue_feature=3.0),
            *_bout_rows(2, 30, 40, red_result="loss", red_feature=2.0, blue_feature=5.0),
            *_bout_rows(3, 50, 60, red_result="no_contest", red_feature=1.0, blue_feature=1.0),
        ]
    )

    result = build_matchup_matrix(long)

    assert result.excluded_no_result == 1
    assert set(result.frame["bout_id"]) == {1, 2}
    assert list(result.frame.sort_values("bout_id")[TARGET_COLUMN]) == ["R", "B"]
    # O alvo é separado das features.
    assert TARGET_COLUMN not in result.feature_columns
    assert result.target_column == TARGET_COLUMN


def test_baseline_corner_vermelho() -> None:
    """CA-04.4: baseline = taxa do corner vermelho pós-exclusão (3R/2B -> 0.6)."""
    long = pd.DataFrame(
        [
            *_bout_rows(1, 10, 20, "win", 4.0, 3.0),
            *_bout_rows(2, 11, 21, "win", 4.0, 3.0),
            *_bout_rows(3, 12, 22, "win", 4.0, 3.0),
            *_bout_rows(4, 13, 23, "loss", 4.0, 3.0),
            *_bout_rows(5, 14, 24, "loss", 4.0, 3.0),
            # Bout NC: não entra no denominador do baseline.
            *_bout_rows(6, 15, 25, "no_contest", 4.0, 3.0),
        ]
    )

    result = build_matchup_matrix(long)

    assert result.excluded_no_result == 1
    assert result.red_corner_win_rate == pytest.approx(0.6)
    # O denominador é a matriz decidida (5 bouts), não os 6 originais.
    assert red_corner_win_rate(result.frame) == pytest.approx(0.6)


def test_derive_target_marca_draw_como_na() -> None:
    """CA-04.3: empate (draw) também vira alvo nulo (excluído a jusante)."""
    pivoted = pd.DataFrame(
        [
            {"bout_id": 1, "result_a": "win", "result_b": "loss"},
            {"bout_id": 2, "result_a": "draw", "result_b": "draw"},
        ]
    )

    targeted = derive_target(pivoted)

    assert targeted.loc[targeted["bout_id"] == 1, TARGET_COLUMN].iloc[0] == "R"
    assert pd.isna(targeted.loc[targeted["bout_id"] == 2, TARGET_COLUMN].iloc[0])


def test_orquestrador_devolve_contrato_coerente() -> None:
    """CA-04: ``build_matchup_matrix`` devolve o dataclass com o contrato completo."""
    long = pd.DataFrame(
        [
            *_bout_rows(1, 10, 20, "win", 4.0, 3.0),
            *_bout_rows(2, 30, 40, "loss", 2.0, 5.0),
        ]
    )

    result = build_matchup_matrix(long)

    assert isinstance(result, MatchupMatrix)
    assert len(result.frame) == 2  # uma linha por bout
    assert result.excluded_no_result == 0
    # O diferencial da feature está entre as colunas de feature; o alvo, não.
    assert f"{_FEATURE}_diff" in result.feature_columns
    assert f"{_FEATURE}_a" in result.feature_columns
    assert TARGET_COLUMN not in result.feature_columns
    # Identidade não é feature.
    assert "fighter_id_a" not in result.feature_columns
    assert "bout_id" not in result.feature_columns
    # Baseline coerente: 1 red win de 2 decididos.
    assert result.red_corner_win_rate == pytest.approx(0.5)


def test_splits_raw_da_luta_corrente_ficam_fora_das_features() -> None:
    """CA-03 (anti-leakage): os splits raw da luta corrente não viram feature; a share as-of, sim.

    Os splits de golpe (``head_landed``, ``reversals``, ``total_strikes_landed``...) são
    box-score do desfecho: têm de entrar em ``_OUTCOME_BASES`` e ficar fora de
    ``feature_columns`` (senão o modelo veria o futuro). Já a derivada as-of
    ``share_head_r3`` (só lutas 1..N-1) sobrevive como feature.
    """
    long = pd.DataFrame(
        [
            {
                **_corner_row(1, Corner.RED, 10, "win", 4.0),
                "head_landed": 20,
                "reversals": 1,
                "total_strikes_landed": 40,
                "share_head_r3": 0.5,
            },
            {
                **_corner_row(1, Corner.BLUE, 20, "loss", 3.0),
                "head_landed": 10,
                "reversals": 0,
                "total_strikes_landed": 25,
                "share_head_r3": 0.4,
            },
        ]
    )

    result = build_matchup_matrix(long)

    # A derivada as-of é feature (nos dois cantos e no diferencial).
    assert "share_head_r3_a" in result.feature_columns
    assert "share_head_r3_b" in result.feature_columns
    assert "share_head_r3_diff" in result.feature_columns
    # Os splits raw da luta corrente NÃO são feature (nem *_a/*_b nem *_diff).
    for base in ("head_landed", "reversals", "total_strikes_landed"):
        assert f"{base}_a" not in result.feature_columns
        assert f"{base}_b" not in result.feature_columns
        assert f"{base}_diff" not in result.feature_columns
        # E o diferencial nem sequer é calculado (fora dos diferenciais).
        assert f"{base}_diff" not in result.frame.columns


def test_cli_stage_matchup_loga_baseline(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-04: o estágio ``matchup`` do CLI produz a matriz e loga o baseline.

    Monkeypatcha o builder da frame longa enriquecida upstream para devolver uma
    frame-fixture -- o estágio roda sem tocar o Postgres. Confirma que a matriz é
    bout-level (uma linha por bout) e que o baseline sai via ``logging`` (``caplog``).
    """
    from ingestion.features import cli

    long = pd.DataFrame(
        [
            *_bout_rows(1, 10, 20, "win", 4.0, 3.0),
            *_bout_rows(2, 30, 40, "win", 4.0, 3.0),
            *_bout_rows(3, 50, 60, "loss", 4.0, 3.0),
        ]
    )
    monkeypatch.setattr(cli, "_enriched_long_frame", lambda _session: long)

    with caplog.at_level(logging.INFO, logger=cli.logger.name):
        frame = cli.run_build(session=object(), stage="matchup")  # type: ignore[arg-type]

    assert len(frame) == 3  # uma linha por bout
    assert TARGET_COLUMN in frame.columns
    mensagens = "\n".join(record.getMessage() for record in caplog.records)
    assert "baseline red=" in mensagens
    assert "0.6667" in mensagens  # 2 red wins de 3 decididos


def test_bloco_a_nao_acrescenta_coluna_crua_a_frame_longa() -> None:
    """CA-04 (RF-02): as cruas do bloco A já existiam e seguem classificadas como desfecho.

    Guarda de caracterização do contrato da frame longa: o bloco A da SPEC 009 é derivação
    pura, sem nenhuma coluna crua nova. As três únicas colunas acrescentadas desde então são
    o contexto de luta do bloco B1/B2 (``weight_class``/``title_bout``/``scheduled_rounds``),
    e elas entram em ``_BOUT_CONTEXT_BASES``, **não** em ``_OUTCOME_BASES``. Se uma slice
    futura projetar uma coluna nova sem classificá-la, este teste (e o da classificação
    exaustiva) fica vermelho -- é o mesmo modo de falha que faria box-score da luta corrente
    virar preditor dela mesma.
    """
    for coluna in _BLOCO_A_COLUNAS_CRUAS:
        assert coluna in LONG_FRAME_COLUMNS, coluna
        assert coluna in _OUTCOME_BASES, coluna

    # O contrato explícito da frame longa: M4/M5 mais o contexto de luta da SPEC 009.
    assert LONG_FRAME_COLUMNS == [
        "fighter_id",
        "fighter_name",
        "event_id",
        "event_name",
        "event_date",
        "bout_id",
        "corner",
        "result",
        "method",
        "round",
        "ending_time_seconds",
        "weight_class",
        "title_bout",
        "scheduled_rounds",
        "knockdowns",
        "sig_strikes_landed",
        "sig_strikes_attempted",
        "takedowns_landed",
        "takedowns_attempted",
        "submission_attempts",
        "control_time_seconds",
        "total_strikes_landed",
        "total_strikes_attempted",
        "head_landed",
        "head_attempted",
        "body_landed",
        "body_attempted",
        "leg_landed",
        "leg_attempted",
        "distance_landed",
        "distance_attempted",
        "clinch_landed",
        "clinch_attempted",
        "ground_landed",
        "ground_attempted",
        "reversals",
        "source",
    ]


def test_derivadas_as_of_do_bloco_a_nao_entram_nas_bases_excluidas() -> None:
    """CA-04 (RF-02): as 12 derivadas as-of do bloco A não são identidade/contexto/desfecho.

    O erro simétrico do teste anterior: classificar uma feature legítima como desfecho a
    excluiria do treino em silêncio, exatamente como as 21 features perdidas do M5.
    """
    for base in _BLOCO_A_BASES:
        assert base not in _NON_FEATURE_BASES, base


# As três colunas cruas de contexto de luta projetadas nesta slice (contexto, não desfecho).
_CONTEXTO_DE_LUTA: tuple[str, ...] = ("weight_class", "title_bout", "scheduled_rounds")


def test_toda_coluna_crua_esta_classificada_em_exatamente_uma_categoria() -> None:
    """CA-07 (RF-02): cada coluna crua é identidade/contexto **ou** desfecho, nunca as duas.

    Guarda executável da RF-02: uma coluna crua nova que ninguém classificou entraria como
    feature preditiva sem que ninguém percebesse -- se for desfecho, isso é vazamento direto.
    A distinção entre contexto e desfecho é semântica e **não** pode ser colapsada:
    ``scheduled_rounds`` é o formato agendado (conhecido antes do gongo), ``round`` é onde a
    luta acabou (só existe depois dela).

    ``corner`` é a única exceção legítima: é consumida pelo pivô (``pivot_corners`` a dropa) e
    por isso nunca chega a ser candidata a feature.

    **Alcance desta guarda: só a frame crua.** ``stance`` não entra por ``read_granular`` --
    nasce depois, em ``trajectory.add_physical_attributes`` -- e por isso não é visível aqui. A
    metade enriquecida da RF-02 é guardada por
    ``tests/features/test_estilo_ponta_a_ponta.py::test_so_as_bases_sao_descartadas_por_serem_string_na_frame_enriquecida``,
    que corre o caminho de produção inteiro e assere que o descarte por string é **exatamente**
    ``stance_a``/``stance_b``. As duas juntas fecham a RF-02; separadas, uma coluna string nova
    da frame enriquecida passaria por este teste sem despertá-lo.
    """
    cruas = set(LONG_FRAME_COLUMNS) - {COL_CORNER}

    assert cruas <= _NON_FEATURE_BASES
    assert _OUTCOME_BASES.isdisjoint(_BOUT_CONTEXT_BASES)
    assert set(_CONTEXTO_DE_LUTA) <= _BOUT_CONTEXT_BASES
    assert {"round", "method", "ending_time_seconds"} <= _OUTCOME_BASES
    assert set(_CONTEXTO_DE_LUTA).isdisjoint(_OUTCOME_BASES)


def test_colapso_do_contexto_de_luta_produz_coluna_unica() -> None:
    """CA-14: o contexto de luta vira coluna única -- sem ``_a``/``_b``, sem ``_diff``.

    O pivô duplica o contexto nos dois cantos porque ele vem da mesma linha de ``bouts``.
    Mantido assim, o ``_diff`` seria zero em toda linha (ruído puro) e a versão string
    (``weight_class_a``) chegaria ao JSONB só para ser descartada em silêncio por
    ``analysis.dataset._numeric_feature_columns`` -- exatamente o modo de falha que custou
    21 features do M5 durante meses.
    """
    long = pd.DataFrame(_bout_rows(1, 10, 20, "win", 4.0, 3.0))

    colapsada = collapse_bout_context_columns(pivot_corners(long))

    for base in _CONTEXTO_DE_LUTA:
        assert base in colapsada.columns, base
        assert f"{base}_a" not in colapsada.columns, base
        assert f"{base}_b" not in colapsada.columns, base
        assert f"{base}{_SUFFIX_DIFF}" not in colapsada.columns, base
    assert list(colapsada["weight_class"]) == ["lightweight"]


def test_colapso_falha_visivel_quando_o_par_nao_existe() -> None:
    """Par de contexto ausente é violação de contrato da frame longa -- falha alto.

    Sem guarda silenciosa: ``LONG_FRAME_COLUMNS`` é contrato, e engolir a ausência faria a
    feature bout-level nascer vazia sem ninguém notar. Mesma disciplina do
    ``validate="one_to_one"`` do pivô.
    """
    with pytest.raises(KeyError):
        collapse_bout_context_columns(pd.DataFrame([{"bout_id": 1}]))


def test_bout_level_nao_gera_diferencial_degenerado_no_orquestrador() -> None:
    """CA-14: nenhuma coluna ``*_diff`` da matriz final tem base de contexto de luta."""
    long = pd.DataFrame(
        [
            *_bout_rows(1, 10, 20, "win", 4.0, 3.0),
            *_bout_rows(2, 30, 40, "loss", 2.0, 5.0),
        ]
    )

    result = build_matchup_matrix(long)

    diffs = [coluna for coluna in result.frame.columns if coluna.endswith(_SUFFIX_DIFF)]
    for coluna in diffs:
        assert coluna[: -len(_SUFFIX_DIFF)] not in _BOUT_CONTEXT_BASES, coluna
    # O PAR CRU de canto nunca é feature, para nenhuma das três colunas de contexto.
    for base in _CONTEXTO_DE_LUTA:
        for sufixo in ("_a", "_b", _SUFFIX_DIFF):
            assert f"{base}{sufixo}" not in result.feature_columns, f"{base}{sufixo}"
    # O rótulo cru colapsado também não é feature -- exceto ``scheduled_rounds``, cuja coluna
    # única É a feature bout-level do bloco B2 (a Slice 03 batizou a feature com o mesmo nome
    # da coluna crua; quem as separa é a allowlist por nome completo, testada à parte).
    for base in (COL_WEIGHT_CLASS, COL_TITLE_BOUT):
        assert base not in result.feature_columns, base
    assert SCHEDULED_ROUNDS in result.feature_columns


def test_orquestrador_emite_as_features_de_divisao_como_coluna_unica() -> None:
    """CA-14/CA-13: as três features de divisão entram no matchup como colunas únicas.

    É a prova de que o caminho bout-level chega até ``feature_columns`` -- o que garante que
    elas serão materializadas no JSONB. Sem par de canto e sem ``_diff``: o contexto vale
    igual para os dois lados, e o diferencial seria zero em toda linha.

    Os valores confirmam a fiação completa: a segunda luta (data posterior, mesma divisão)
    enxerga a primeira, que terminou em finalização -> taxa base 1,0.
    """
    long = pd.DataFrame(
        [
            *_bout_rows(1, 10, 20, "win", 4.0, 3.0),
            *_bout_rows(2, 30, 40, "win", 2.0, 5.0),
        ]
    )
    long.loc[long["bout_id"] == 1, "method"] = BoutMethod.KO_TKO.value
    long.loc[long["bout_id"] == 2, "event_date"] = date(2023, 6, 1)

    result = build_matchup_matrix(long)

    for coluna in (*DIVISION_CATEGORY_FEATURES, DIVISION_FINISH_RATE_PRIOR):
        assert coluna in result.feature_columns, coluna
        assert f"{coluna}_a" not in result.frame.columns, coluna
        assert f"{coluna}{_SUFFIX_DIFF}" not in result.frame.columns, coluna

    segunda = result.frame.loc[result.frame["bout_id"] == 2].iloc[0]
    assert segunda[WEIGHT_CLASS_LBS] == pytest.approx(155.0)  # lightweight
    assert segunda[IS_WOMENS_DIVISION] == pytest.approx(0.0)
    assert segunda[DIVISION_FINISH_RATE_PRIOR] == pytest.approx(1.0)
    primeira = result.frame.loc[result.frame["bout_id"] == 1].iloc[0]
    assert pd.isna(primeira[DIVISION_FINISH_RATE_PRIOR])  # primeira data da divisão


def test_taxa_base_de_divisao_conta_lutas_nc_antes_da_exclusao() -> None:
    """A ordem em ``build_matchup_matrix`` é load-bearing: divisão ANTES de excluir NC/draw.

    Uma luta sem resultado aconteceu naquela divisão e não terminou em finalização. Excluí-la
    antes de calcular a taxa base mudaria o denominador em silêncio e divergiria da convenção
    de ``rolling.FINISH_RATE_PRIOR``, que também conta NC/empate no denominador.
    """
    long = pd.DataFrame(
        [
            *_bout_rows(1, 10, 20, "win", 4.0, 3.0),
            *_bout_rows(2, 30, 40, "no_contest", 2.0, 5.0),
            *_bout_rows(3, 50, 60, "win", 2.0, 5.0),
        ]
    )
    long.loc[long["bout_id"] == 1, "method"] = BoutMethod.KO_TKO.value
    long.loc[long["bout_id"] == 2, "method"] = BoutMethod.NO_CONTEST.value
    long.loc[long["bout_id"] == 2, "event_date"] = date(2023, 3, 1)
    long.loc[long["bout_id"] == 3, "event_date"] = date(2023, 6, 1)

    result = build_matchup_matrix(long)

    assert result.excluded_no_result == 1
    terceira = result.frame.loc[result.frame["bout_id"] == 3].iloc[0]
    # 1 finalização em 2 lutas anteriores -- o NC entra no denominador, não some.
    assert terceira[DIVISION_FINISH_RATE_PRIOR] == pytest.approx(0.5)


def test_cli_stage_matchup_loga_as_colunas_bout_level(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-14 (demonstração): o estágio ``matchup`` nomeia as bout-level como colunas únicas.

    O valor demonstrável da slice é justamente "as features de divisão aparecem sem
    ``_a``/``_b``", e o ``head().to_string()`` do preview trunca com 60+ colunas -- sem uma
    linha de log **dedicada** a demonstração ficaria invisível na execução real. Por isso a
    asserção é sobre a linha própria (identificada pelo rótulo), não sobre "o nome aparece em
    algum lugar do log": num fixture pequeno o preview mostraria as colunas por acidente e o
    teste passaria sem que a linha existisse.
    """
    from ingestion.features import cli

    long = pd.DataFrame(_bout_rows(1, 10, 20, "win", 4.0, 3.0))
    monkeypatch.setattr(cli, "_enriched_long_frame", lambda _session: long)

    with caplog.at_level(logging.INFO, logger=cli.logger.name):
        cli.run_build(session=object(), stage="matchup")  # type: ignore[arg-type]

    linhas = [
        record.getMessage()
        for record in caplog.records
        if _ROTULO_BOUT_LEVEL in record.getMessage()
    ]
    assert len(linhas) == 1
    for coluna in (*DIVISION_CATEGORY_FEATURES, DIVISION_FINISH_RATE_PRIOR):
        assert coluna in linhas[0], coluna


def test_orquestrador_emite_as_features_de_formato_como_coluna_unica() -> None:
    """CA-14/CA-13: ``is_title_bout`` e ``scheduled_rounds`` entram como coluna única.

    Bloco B2 da SPEC 009 (Slice 03). O formato agendado vale para a luta inteira: emiti-lo
    como par de canto produziria um ``_diff`` identicamente zero em toda linha (ruído puro),
    e o par cru ``scheduled_rounds_a``/``_b`` chegaria ao JSONB só para diluir o modelo.

    ``scheduled_rounds`` é **contexto conhecido antes do gongo**, distinto de ``round`` (onde
    a luta acabou, que é desfecho e segue excluído -- RF-02).
    """
    long = pd.DataFrame(_bout_rows(1, 10, 20, "win", 4.0, 3.0, title_bout=True, scheduled_rounds=5))

    result = build_matchup_matrix(long)

    for coluna in FORMAT_FEATURES:
        assert coluna in result.feature_columns, coluna
        for sufixo in ("_a", "_b", _SUFFIX_DIFF):
            assert f"{coluna}{sufixo}" not in result.frame.columns, f"{coluna}{sufixo}"
    linha = result.frame.iloc[0]
    assert linha[IS_TITLE_BOUT] == pytest.approx(1.0)
    assert linha[SCHEDULED_ROUNDS] == pytest.approx(5.0)


def test_formato_sem_titulo_e_de_tres_rounds_produz_zero_e_tres() -> None:
    """CA-14: ``title_bout=False`` -> ``0.0`` e ``scheduled_rounds=3`` -> ``3.0``.

    Zero aqui é valor **medido** (a luta não vale cinturão), não ausência -- o par com o
    teste de nulo abaixo é o que separa as duas semânticas.
    """
    long = pd.DataFrame(
        _bout_rows(1, 10, 20, "win", 4.0, 3.0, title_bout=False, scheduled_rounds=3)
    )

    linha = build_matchup_matrix(long).frame.iloc[0]

    assert linha[IS_TITLE_BOUT] == pytest.approx(0.0)
    assert linha[SCHEDULED_ROUNDS] == pytest.approx(3.0)


def test_formato_nulo_na_origem_permanece_nulo_nunca_zero() -> None:
    """CA-08 (RF-03): contexto ausente vira feature ausente -- jamais zero por imputação.

    ``scheduled_rounds`` está preenchido em 93,1% do banco real: os 6,9% restantes precisam
    chegar ao modelo como ausência honesta. Zero em ``is_title_bout`` afirmaria "não vale
    cinturão" sobre uma luta cujo formato ninguém registrou, e zero em ``scheduled_rounds``
    seria um formato impossível.
    """
    long = pd.DataFrame(
        _bout_rows(1, 10, 20, "win", 4.0, 3.0, title_bout=None, scheduled_rounds=None)
    )

    linha = build_matchup_matrix(long).frame.iloc[0]

    assert pd.isna(linha[IS_TITLE_BOUT])
    assert pd.isna(linha[SCHEDULED_ROUNDS])


def test_allowlist_bout_level_tem_precedencia_sobre_a_exclusao_por_base() -> None:
    """CA-13/CA-14: a feature ``scheduled_rounds`` sobrevive à base homônima excluída.

    O ponto de falha silenciosa desta slice. A SPEC batiza a feature bout-level com o
    **mesmo nome** da coluna crua, que está em ``_BOUT_CONTEXT_BASES`` e portanto em
    ``_NON_FEATURE_BASES``. Como ``_base_of("scheduled_rounds") == "scheduled_rounds"``, a
    exclusão por base descartaria a feature **sem erro nenhum** -- o mesmo modo de falha que
    escondeu 21 features do M5 por meses. A allowlist consultada por **nome completo**, antes
    da exclusão por base, é o que separa a coluna única (feature) do par de canto (contexto).
    """
    long = pd.DataFrame(_bout_rows(1, 10, 20, "win", 4.0, 3.0, title_bout=True, scheduled_rounds=5))

    result = build_matchup_matrix(long)

    assert SCHEDULED_ROUNDS in BOUT_LEVEL_FEATURE_COLUMNS
    assert SCHEDULED_ROUNDS in _NON_FEATURE_BASES  # a base homônima continua excluída
    assert SCHEDULED_ROUNDS in result.feature_columns
    # O par cru continua fora: só a coluna única é feature.
    for sufixo in ("_a", "_b", _SUFFIX_DIFF):
        assert f"{SCHEDULED_ROUNDS}{sufixo}" not in result.feature_columns


def test_contexto_de_luta_nao_vira_base_numerica_de_diferencial() -> None:
    """CA-14: ``numeric_feature_bases`` não devolve o contexto de luta como base.

    Depois do colapso o par ``*_a``/``*_b`` do contexto nem existe, então nenhuma base de
    contexto pode aparecer -- e nenhum ``_diff`` degenerado nasce dele.
    """
    long = pd.DataFrame(_bout_rows(1, 10, 20, "win", 4.0, 3.0, title_bout=True, scheduled_rounds=5))

    bases = numeric_feature_bases(collapse_bout_context_columns(pivot_corners(long)))

    assert set(bases).isdisjoint(_BOUT_CONTEXT_BASES)
    assert _FEATURE in bases


def test_nomes_do_bloco_b2_nao_colidem_com_as_bases_proscritas() -> None:
    """CA-12 (RF-10): nenhum nome do bloco B2 é média de carreira proscrita do snapshot 2025.

    ``splm``, ``str_acc``, ``sapm``... embutem o resultado da luta que se quer prever
    (ADR 0002). Uma feature point-in-time legítima com nome parecido confundiria leitor e
    guarda -- por isso a checagem cobre também as variantes de canto.
    """
    novos = (IS_TITLE_BOUT, SCHEDULED_ROUNDS, FIVE_ROUND_BOUTS_BEFORE)

    for nome in novos:
        assert nome not in PROSCRIBED_FEATURE_BASES, nome
        for sufixo in ("_a", "_b", _SUFFIX_DIFF):
            assert f"{nome}{sufixo}" not in PROSCRIBED_FEATURE_BASES, f"{nome}{sufixo}"


# As 8 bases as-of do bloco C da SPEC 009 (Slice 04) e as 4 colunas cruas de onde saem.
_BLOCO_C_BASES: tuple[str, ...] = (
    KO_LOSSES_PRIOR,
    SUBMISSION_LOSSES_PRIOR,
    CAREER_MINUTES_BEFORE,
    SIG_STRIKES_LANDED_PM_TREND,
    SIG_STRIKES_ABSORBED_PM_TREND,
    TAKEDOWNS_LANDED_AVG_TREND,
    TAKEDOWN_DEFENSE_TREND,
    CONTROL_TIME_AVG_TREND,
)
_BLOCO_C_COLUNAS_CRUAS: tuple[str, ...] = ("result", "method", "round", "ending_time_seconds")


def test_bloco_c_nao_acrescenta_coluna_crua_a_frame_longa() -> None:
    """CA-06 (RF-02): as cruas do bloco C já existiam e seguem classificadas como desfecho.

    ``method`` (como a luta acabou), ``round`` e ``ending_time_seconds`` (onde ela acabou) são
    desfecho da própria luta -- só as derivadas as-of do bloco C entram como preditor. O
    contraste com ``scheduled_rounds``, que é contexto pré-gongo, é justamente o que a RF-02
    proíbe colapsar.
    """
    for coluna in _BLOCO_C_COLUNAS_CRUAS:
        assert coluna in LONG_FRAME_COLUMNS, coluna
        assert coluna in _OUTCOME_BASES, coluna
        assert coluna not in _BOUT_CONTEXT_BASES, coluna


def test_derivadas_as_of_do_bloco_c_nao_entram_nas_bases_excluidas() -> None:
    """CA-06 (RF-02): as 8 derivadas as-of do bloco C não são identidade/contexto/desfecho.

    O erro simétrico: classificar uma feature legítima como desfecho a excluiria do treino em
    silêncio, exatamente como as 21 features perdidas do M5. Nenhuma delas é bout-level -- o
    bloco C é todo por lutador, e cada base vira o trio ``_a``/``_b``/``_diff``.
    """
    for base in _BLOCO_C_BASES:
        assert base not in _NON_FEATURE_BASES, base
        assert base not in BOUT_LEVEL_FEATURE_COLUMNS, base


# As duas bases as-of dos blocos D3+D4 da SPEC 009 (Slice 06). Todo o bloco é derivação: as
# colunas cruas que ele lê (``result`` do lutador e os eixos de estilo do adversário) já
# existiam -- as primeiras como desfecho, os segundos como feature as-of da linha do rival.
_BASES_D3_D4: tuple[str, ...] = (SIMILAR_STYLE_WIN_RATE_PRIOR, OPPONENT_WIN_RATE_PRIOR_AVG)


def test_bloco_d3_d4_nao_acrescenta_coluna_crua_a_frame_longa() -> None:
    """CA-07 (RF-02): a slice do D3/D4 é derivação pura -- nenhuma coluna crua nova.

    Guarda de caracterização: as duas features nascem do que a frame longa já carregava
    (``result``, classificado como desfecho) mais o estado as-of lido da linha do adversário.
    Se uma slice futura projetar coluna crua nova sem classificá-la, este teste e o da
    classificação exaustiva ficam vermelhos -- é o mesmo modo de falha que faria box-score da
    luta corrente virar preditor dela mesma.
    """
    assert "result" in LONG_FRAME_COLUMNS
    assert "result" in _OUTCOME_BASES
    assert "result" not in _BOUT_CONTEXT_BASES


def test_derivadas_as_of_do_bloco_d3_d4_nao_entram_nas_bases_excluidas() -> None:
    """CA-07 (RF-02): as duas derivadas as-of do D3/D4 não são identidade/contexto/desfecho.

    O erro simétrico do teste anterior: classificar uma feature legítima como desfecho a
    excluiria do treino em silêncio, exatamente como as 21 features perdidas do M5. Nenhuma
    das duas é bout-level -- as duas são **do lutador** e viram o trio ``_a``/``_b``/``_diff``.
    """
    for base in _BASES_D3_D4:
        assert base not in _NON_FEATURE_BASES, base
        assert base not in BOUT_LEVEL_FEATURE_COLUMNS, base
