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

import pandas as pd
import pytest
from pandas.errors import MergeError

from apps.bouts.enums import Corner
from apps.fighters.enums import Stance
from ingestion.features.matchup import (
    TARGET_COLUMN,
    MatchupMatrix,
    add_differentials,
    build_matchup_matrix,
    derive_target,
    numeric_feature_bases,
    pivot_corners,
    red_corner_win_rate,
)

_FEATURE = "sig_strikes_pm_asof"


def _corner_row(
    bout_id: int,
    corner: Corner,
    fighter_id: int,
    result: str,
    feature: float,
) -> dict[str, object]:
    """Uma linha lutador-luta mínima da frame longa (uma participação)."""
    return {
        "bout_id": bout_id,
        "corner": corner,
        "fighter_id": fighter_id,
        "result": result,
        _FEATURE: feature,
    }


def _bout_rows(
    bout_id: int,
    red_fighter_id: int,
    blue_fighter_id: int,
    red_result: str,
    red_feature: float,
    blue_feature: float,
) -> list[dict[str, object]]:
    """Duas linhas (red/blue) de um bout; o resultado do azul é o espelho do vermelho."""
    blue_result = {"win": "loss", "loss": "win"}.get(red_result, red_result)
    return [
        _corner_row(bout_id, Corner.RED, red_fighter_id, red_result, red_feature),
        _corner_row(bout_id, Corner.BLUE, blue_fighter_id, blue_result, blue_feature),
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
