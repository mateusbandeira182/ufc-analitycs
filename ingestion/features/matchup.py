"""Matriz de confronto (matchup) bout-level com diferenciais, alvo e baseline.

Núcleo da Slice 04 da SPEC 005 (M4 -- prontidão preditiva). Pivota a frame longa
por lutador-luta (já enriquecida pelas Slices 02/03) de volta para **uma linha por
bout**, com as features as-of dos dois cantos (``*_a`` = red, ``*_b`` = blue), os
diferenciais (A menos B, ``*_diff``), a coluna-alvo ``winner_corner`` **separada**
das features, e o **baseline ingênuo** (taxa de vitória do corner vermelho) --
estatística descritiva, sem treino nem split.

Convenção fixa: **A = red, B = blue** (vernáculo do octógono; ADR 0001). O baseline
mede exatamente ``P(winner_corner == "R")`` -- o ~0,58 esperado depende dessa
convenção.

Contrato de entrada (Slice 01, reconciliado contra o código real): a frame longa
carrega ``result`` por canto (win/loss/no_contest/draw), **não** ``winner_id`` (ver
``LONG_FRAME_COLUMNS``). O alvo é derivado de ``result_a`` -- que já distingue
NC/draw -- em vez de comparar ``winner_id`` com ``fighter_id`` (o snippet do plano
foi escrito contra um contrato presumido). NC/draw são excluídos e contabilizados
(decisão #4 da SPEC).

Column-agnóstico para features: o módulo não lista feature por feature. Deriva os
diferenciais e as colunas de feature das colunas que sobrevivem ao pivô, excluindo
um conjunto conhecido e estável de identidade/contexto/desfecho (``_NON_FEATURE_BASES``)
-- assim sobrevive a Slices 02/03 acrescentarem/renomearem features as-of. O
box-score bruto da própria luta (golpes, quedas, control time do bout corrente) é
desfecho, não predição as-of: fica fora das features (anti-leakage, RF-05/RNF).

O DataFrame do Pandas é fronteira dinâmica (``pyproject.toml`` marca ``pandas.*``
como ``follow_imports=skip``): as funções públicas recebem/devolvem ``pd.DataFrame``
tipado. Esta slice **não** persiste nada -- a materialização é a Slice 05.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd
from pandas.api.types import is_numeric_dtype

from apps.bouts.enums import Corner

logger = logging.getLogger(__name__)

# Convenção do octógono: canto A = red, canto B = blue.
_SUFFIX_A = "_a"
_SUFFIX_B = "_b"
_SUFFIX_DIFF = "_diff"

# Colunas-chave da frame longa consumidas por esta slice (contrato estável Slice 01).
COL_BOUT_ID = "bout_id"
COL_CORNER = "corner"
COL_RESULT = "result"

# Coluna-alvo, separada das features (domínio "R" | "B"; NA em NC/draw -> excluído).
TARGET_COLUMN = "winner_corner"
_CORNER_RED = "R"
_CORNER_BLUE = "B"

# Valores da coluna ``result`` (ver ``ingestion.features.long_frame.BoutResult``).
_RESULT_WIN = "win"
_RESULT_LOSS = "loss"

# ``result_a``/``result_b`` após o pivô (o alvo é lido do canto vermelho, A).
_COL_RESULT_A = f"{COL_RESULT}{_SUFFIX_A}"

# Bases que NÃO são features preditivas as-of e por isso ficam fora dos diferenciais
# e de ``feature_columns``. Fonte única do conhecimento sobre o que é ruído/leakage.
_IDENTITY_BASES: frozenset[str] = frozenset({COL_BOUT_ID, "fighter_id", "fighter_name"})
_EVENT_CONTEXT_BASES: frozenset[str] = frozenset({"event_id", "event_name", "event_date", "source"})
# Desfecho da própria luta (não é predição as-of -- leakage se usado como feature).
# Inclui os splits de golpe wide do M5 (Sprint 02): são box-score da luta corrente, não
# predição. Só as derivadas as-of (``share_*_r3``, ``round1_*_r3``) entram como feature.
_OUTCOME_BASES: frozenset[str] = frozenset(
    {
        COL_RESULT,
        "method",
        "round",
        "ending_time_seconds",
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
    }
)
_NON_FEATURE_BASES: frozenset[str] = _IDENTITY_BASES | _EVENT_CONTEXT_BASES | _OUTCOME_BASES


@dataclass(frozen=True)
class MatchupMatrix:
    """Contrato de saída da matriz de confronto (consumido pelo CLI e pela Slice 05).

    ``frame`` tem uma linha por bout decidido: ``*_a`` / ``*_b`` / ``*_diff`` mais a
    coluna-alvo ``winner_corner``. ``feature_columns`` lista as colunas de feature
    (``*_a`` / ``*_b`` / ``*_diff``) e **não** inclui o alvo. ``excluded_no_result`` é
    o número de lutas NC/draw removidas. ``red_corner_win_rate`` é o baseline ingênuo.
    """

    frame: pd.DataFrame
    feature_columns: list[str]
    target_column: str
    excluded_no_result: int
    red_corner_win_rate: float


def pivot_corners(long: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por lutador-luta -> uma linha por bout, canto A = red / B = blue.

    Separa a frame por canto, descarta a coluna ``corner`` e junta os dois cantos por
    ``bout_id`` com sufixos ``(_a, _b)`` e ``validate="one_to_one"``: uma luta
    bem-formada tem exatamente um red e um blue; um bout com canto duplicado levanta
    ``MergeError`` (dado corrompido falha visível, alinhado ao risco de série
    fragmentada da SPEC).
    """
    red = long[long[COL_CORNER] == Corner.RED].drop(columns=[COL_CORNER])
    blue = long[long[COL_CORNER] == Corner.BLUE].drop(columns=[COL_CORNER])
    return red.merge(blue, on=COL_BOUT_ID, suffixes=(_SUFFIX_A, _SUFFIX_B), validate="one_to_one")


def numeric_feature_bases(matrix: pd.DataFrame) -> list[str]:
    """Bases com par ``*_a``/``*_b`` numérico que são features (fora de identidade/desfecho).

    Percorre as colunas ``*_a``, confirma a existência do par ``*_b``, exige dtype
    numérico nas duas e exclui as bases conhecidas de identidade/contexto/desfecho. A
    ordem de descoberta (ordem das colunas) é preservada -- determinística.
    """
    bases: list[str] = []
    for column in matrix.columns:
        if not column.endswith(_SUFFIX_A):
            continue
        base = column[: -len(_SUFFIX_A)]
        if base in _NON_FEATURE_BASES:
            continue
        column_b = f"{base}{_SUFFIX_B}"
        if column_b not in matrix.columns:
            continue
        if is_numeric_dtype(matrix[column]) and is_numeric_dtype(matrix[column_b]):
            bases.append(base)
    return bases


def add_differentials(matrix: pd.DataFrame, bases: list[str]) -> pd.DataFrame:
    """Adiciona ``<base>_diff = <base>_a - <base>_b`` para cada base numérica.

    Subtração vetorizada coluna a coluna; tipos nullable (``Int64``) propagam ``NA``
    numa estreia (feature as-of ausente). A frame de entrada não é mutada.
    """
    matrix = matrix.copy()
    for base in bases:
        matrix[f"{base}{_SUFFIX_DIFF}"] = (
            matrix[f"{base}{_SUFFIX_A}"] - matrix[f"{base}{_SUFFIX_B}"]
        )
    return matrix


def derive_target(matrix: pd.DataFrame) -> pd.DataFrame:
    """Adiciona ``winner_corner`` (R/B) a partir do resultado do canto vermelho (A).

    ``result_a == "win"`` -> ``R``; ``result_a == "loss"`` -> ``B``; qualquer outro
    valor (no-contest/draw) -> ``NA`` (excluído a jusante). A frame de entrada não é
    mutada.
    """
    result_a = matrix[_COL_RESULT_A]
    corner = pd.Series(pd.NA, index=matrix.index, dtype="string")
    corner[result_a == _RESULT_WIN] = _CORNER_RED
    corner[result_a == _RESULT_LOSS] = _CORNER_BLUE
    return matrix.assign(**{TARGET_COLUMN: corner})


def red_corner_win_rate(matrix: pd.DataFrame) -> float:
    """Baseline ingênuo: fração de lutas em que o corner vermelho vence (``== "R"``).

    Calculado sobre a matriz **decidida** (pós-exclusão de NC/draw) -- NC/draw nunca
    entram no denominador. É estatística descritiva: usa só o alvo, jamais as features.
    """
    return float((matrix[TARGET_COLUMN] == _CORNER_RED).mean())


def _feature_columns(matrix: pd.DataFrame) -> list[str]:
    """Colunas de feature: tudo menos o alvo e as bases de identidade/contexto/desfecho."""
    return [
        column
        for column in matrix.columns
        if column != TARGET_COLUMN and _base_of(column) not in _NON_FEATURE_BASES
    ]


def _base_of(column: str) -> str:
    """Remove o sufixo de canto/diferencial (``_a``/``_b``/``_diff``) para obter a base."""
    for suffix in (_SUFFIX_DIFF, _SUFFIX_A, _SUFFIX_B):
        if column.endswith(suffix):
            return column[: -len(suffix)]
    return column


def build_matchup_matrix(long: pd.DataFrame) -> MatchupMatrix:
    """Encadeia pivô -> diferenciais -> alvo -> exclusão -> baseline e devolve o contrato.

    Excluir NC/draw acontece **antes** do baseline (não poluem o denominador). O
    resultado é o dataclass ``MatchupMatrix`` consumido pelo CLI e (na Slice 05) pela
    materialização.
    """
    matrix = pivot_corners(long)
    matrix = add_differentials(matrix, numeric_feature_bases(matrix))
    matrix = derive_target(matrix)

    total = len(matrix)
    decided = matrix[matrix[TARGET_COLUMN].notna()].reset_index(drop=True)
    excluded = total - len(decided)

    return MatchupMatrix(
        frame=decided,
        feature_columns=_feature_columns(decided),
        target_column=TARGET_COLUMN,
        excluded_no_result=excluded,
        red_corner_win_rate=red_corner_win_rate(decided),
    )
