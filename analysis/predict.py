"""Serving do modelo preditivo: probabilidade de vitória num confronto hipotético A vs B.

Peça de servir da fase 2. ``predict_matchup`` recebe dois ``fighter_id`` e devolve a
probabilidade de cada canto vencer um confronto **hipotético** "as-of agora" (não uma luta
persistida). Reusa ao máximo a engenharia de features point-in-time do M4/M5
(``ingestion.features.{long_frame,rolling,trajectory,matchup}``): em vez de reimplementar o
cálculo as-of, injeta uma **luta sintética** (A no canto vermelho, B no azul) datada depois
de todas as lutas reais e roda a mesma pipeline. Como cada feature usa ``shift(1)`` (exclui a
luta corrente), a linha sintética de cada lutador é calculada a partir de **todas** as suas
lutas passadas 1..N -- exatamente o estado "agora". O diff A-B da linha sintética é montado no
mesmo formato que ``matchup`` produz para o treino e alinhado às ``feature_names`` do modelo.

Convenção fixa A = red, B = blue (ADR 0001): o modelo prevê ``P(winner_corner == red)``, que
aqui é ``P(A vence)``. Anti-leakage/``_safe_ratio`` (denominador zero -> ``NaN``, nunca
``inf``) e a degradação para ``NaN`` de features não-backfilladas (round-a-round) são herdados
da pipeline; o ``HistGradientBoostingClassifier`` trata ``NaN`` nativamente. Nada é escrito no
banco (leitura pura sobre o granular).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from analysis.model import ARTIFACTS_DIR, LoadedModel, load_artifact
from apps.bouts.enums import BoutMethod, Corner
from ingestion.features.long_frame import LONG_FRAME_COLUMNS, build_long_frame, read_granular
from ingestion.features.matchup import (
    COL_BOUT_ID,
    COL_CORNER,
    COL_RESULT,
    add_differentials,
    numeric_feature_bases,
    pivot_corners,
)
from ingestion.features.rolling import (
    COL_FIGHTER_ID,
    add_recent_form_features,
    add_round_dynamics_features,
)
from ingestion.features.trajectory import (
    COL_EVENT_DATE,
    add_trajectory_features,
    load_fighters_bio,
    load_round_stats,
)

logger = logging.getLogger(__name__)

# Sentinela da luta hipotética: ids negativos nunca colidem com ids reais (seriais > 0).
_HYPOTHETICAL_BOUT_ID = -1
_HYPOTHETICAL_EVENT_ID = -1

# Alvo binário do modelo: canto vermelho = 1 (ver ``analysis.dataset``). A = red, logo a
# probabilidade de A vencer é a da classe 1.
_RED_LABEL = 1


@dataclass(frozen=True)
class MatchupPrediction:
    """Resultado da predição de um confronto hipotético A vs B.

    ``prob_a_wins``/``prob_b_wins`` são complementares (somam 1); ``predicted_winner_id`` é o
    ``fighter_id`` do canto de maior probabilidade.
    """

    prob_a_wins: float
    prob_b_wins: float
    predicted_winner_id: int


def _hypothetical_rows(
    fighter_a_id: int, fighter_b_id: int, as_of: date
) -> list[dict[str, object]]:
    """Duas linhas long (A=red, B=blue) da luta sintética, datadas em ``as_of``.

    Só identidade/canto/data/resultado importam: o box-score da luta corrente é ``None`` (a
    luta é futura) e é descartado pelo ``shift(1)`` da pipeline -- as features da linha
    sintética vêm apenas das lutas passadas do lutador. ``result`` é ``no_contest`` (placeholder
    inofensivo: também excluído da própria linha pelo ``shift(1)``).
    """
    base: dict[str, object] = dict.fromkeys(LONG_FRAME_COLUMNS)
    base.update(
        {
            COL_BOUT_ID: _HYPOTHETICAL_BOUT_ID,
            "event_id": _HYPOTHETICAL_EVENT_ID,
            "event_name": "hypothetical",
            COL_EVENT_DATE: as_of,
            COL_RESULT: "no_contest",
            "method": BoutMethod.NO_CONTEST.value,
            "source": "prediction",
        }
    )
    red = {**base, COL_FIGHTER_ID: fighter_a_id, "fighter_name": "A", COL_CORNER: Corner.RED}
    blue = {**base, COL_FIGHTER_ID: fighter_b_id, "fighter_name": "B", COL_CORNER: Corner.BLUE}
    return [red, blue]


def _require_history(long: pd.DataFrame, fighter_id: int, label: str) -> None:
    """Falha visível se o lutador não tem nenhuma luta no granular (sem features as-of).

    Predizer sem histórico produziria um vetor de features inteiramente ``NaN`` -- em vez de
    fabricar uma predição vazia, o erro é explícito (o que falta: lutas semeadas para esse id).
    """
    if not bool((long[COL_FIGHTER_ID] == fighter_id).any()):
        raise ValueError(
            f"Lutador {label} (id={fighter_id}) não tem histórico de lutas no granular; "
            f"sem lutas passadas não há features as-of para predizer."
        )


def _asof_matchup_row(session: Session, fighter_a_id: int, fighter_b_id: int) -> pd.DataFrame:
    """Constrói a linha bout-level (``*_a``/``*_b``/``*_diff``) do confronto as-of agora.

    Lê o granular, injeta a luta sintética A vs B datada depois de tudo, roda a pipeline
    completa de features point-in-time (forma recente + trajetória + dinâmica por round) e
    pivota apenas a luta sintética para uma linha com os diferenciais -- o mesmo formato que
    ``matchup`` entrega ao treino, sem o alvo (a luta é hipotética).
    """
    frames = read_granular(session)
    long = build_long_frame(frames)
    _require_history(long, fighter_a_id, "A")
    _require_history(long, fighter_b_id, "B")

    as_of = datetime.now(UTC).date()
    synthetic = pd.DataFrame(_hypothetical_rows(fighter_a_id, fighter_b_id, as_of))
    combined = pd.concat([long, synthetic], ignore_index=True)

    combined = add_recent_form_features(combined)
    fighters_bio = load_fighters_bio(session.connection())
    combined = add_trajectory_features(combined, fighters_bio)
    round_stats = load_round_stats(session.connection())
    combined = add_round_dynamics_features(combined, round_stats)

    only_synthetic = combined[combined[COL_BOUT_ID] == _HYPOTHETICAL_BOUT_ID]
    pivoted = pivot_corners(only_synthetic)
    return add_differentials(pivoted, numeric_feature_bases(pivoted))


def _align_features(matchup_row: pd.DataFrame, feature_names: list[str]) -> pd.DataFrame:
    """Alinha a linha de confronto às ``feature_names`` do modelo (mesma ordem, ``float64``).

    Espelha ``analysis.dataset.build_dataset``: seleciona exatamente as colunas do treino (as
    ausentes -- ex.: feature 100% ``NaN`` descartada no treino -- viram coluna ``NaN``),
    converte para numérico e ``float64``, preservando o ``NaN`` explícito (sem imputação). O
    ``HistGradientBoostingClassifier`` trata a ausência nativamente.
    """
    aligned = matchup_row.reindex(columns=feature_names)
    numeric: pd.DataFrame = aligned.apply(pd.to_numeric).astype("float64")
    return numeric


def predict_matchup(
    session: Session,
    fighter_a_id: int,
    fighter_b_id: int,
    directory: Path = ARTIFACTS_DIR,
) -> MatchupPrediction:
    """Prediz a probabilidade de A vencer um confronto hipotético A vs B "as-of agora".

    Carrega o modelo persistido (``directory``), constrói o vetor de features do confronto
    reusando a engenharia point-in-time (estado 1..N de cada lutador via luta sintética) e
    devolve as probabilidades complementares e o vencedor previsto. Convenção A = red: a
    probabilidade da classe 1 (canto vermelho) é a de A vencer. Levanta ``ValueError`` se um
    dos lutadores não tem histórico, e ``FileNotFoundError`` se não há artefato treinado.
    """
    loaded: LoadedModel = load_artifact(directory)
    matchup_row = _asof_matchup_row(session, fighter_a_id, fighter_b_id)
    features = _align_features(matchup_row, loaded.feature_names)

    proba = loaded.model.predict_proba(features)
    red_index = list(loaded.model.classes_).index(_RED_LABEL)
    prob_a_wins = float(proba[0, red_index])
    prob_b_wins = float(1.0 - prob_a_wins)
    predicted_winner_id = fighter_a_id if prob_a_wins >= prob_b_wins else fighter_b_id
    return MatchupPrediction(
        prob_a_wins=prob_a_wins,
        prob_b_wins=prob_b_wins,
        predicted_winner_id=predicted_winner_id,
    )
