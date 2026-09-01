"""Leitura das predições registradas confrontadas com o resultado real (sem efeito colateral).

O acerto é **derivado** aqui, a cada leitura: compara ``bout_predictions.predicted_winner_id``
com ``bouts.winner_id``, a única fonte da verdade do resultado. Nunca é lido de coluna --
persistir "acertou" seria pré-agregação destrutiva e ficaria silenciosamente errado se um
resultado fosse corrigido depois (princípio de granularidade do ``CLAUDE.md``, ADR 0001).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.models import Bout
from apps.predictions.models import BoutPrediction


@dataclass(frozen=True)
class PredictionOutcome:
    """Uma predição confrontada com o resultado real, derivado sob demanda.

    ``hit`` é ``None`` -- não ``False`` -- quando não há vencedor a comparar de um dos
    lados: ``bouts.winner_id`` nulo (luta ainda sem resultado, empate ou no contest) ou
    predição sem vencedor previsto. Sem gabarito não há acerto nem erro, e a linha fica
    fora do denominador de qualquer métrica calculada depois.
    """

    prediction: BoutPrediction
    actual_winner_id: int | None
    hit: bool | None


def _derive_hit(predicted_winner_id: int | None, actual_winner_id: int | None) -> bool | None:
    """Compara previsto x ocorrido; ``None`` quando falta vencedor de qualquer um dos lados."""
    if predicted_winner_id is None or actual_winner_id is None:
        return None
    return predicted_winner_id == actual_winner_id


def get_prediction_outcomes(session: Session, bout_id: int) -> list[PredictionOutcome]:
    """Predições registradas para uma luta, cada uma com o acerto derivado do resultado.

    Ordem determinística por ``predicted_at`` (desempate por ``id``), para a série por
    versão de modelo ser estável entre leituras. Nunca escreve; nunca lê acerto de coluna.
    """
    stmt = (
        select(BoutPrediction, Bout.winner_id)
        .join(Bout, Bout.id == BoutPrediction.bout_id)
        .where(BoutPrediction.bout_id == bout_id)
        .order_by(BoutPrediction.predicted_at.asc(), BoutPrediction.id.asc())
    )
    return [
        PredictionOutcome(
            prediction=prediction,
            actual_winner_id=winner_id,
            hit=_derive_hit(prediction.predicted_winner_id, winner_id),
        )
        for prediction, winner_id in session.execute(stmt).tuples().all()
    ]
