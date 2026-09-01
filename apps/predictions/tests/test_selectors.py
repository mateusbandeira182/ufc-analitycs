"""Testes da leitura do previsto x ocorrido (Selector) contra o Postgres transacional.

O acerto é **derivado** de ``bouts.winner_id`` (fonte da verdade do resultado) contra
``predicted_winner_id``, calculado a cada leitura -- nunca lido de coluna. Os três estados
importam: acertou, errou, e "não dá para saber" (luta ainda sem resultado, empate ou no
contest), este último ``None`` e jamais ``False``.

As predições são semeadas pelo caminho de escrita real da slice (``record_prediction``),
que dispensa uma factory dedicada de ``BoutPrediction`` sem segundo callsite.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from apps.bouts.tests.factories import BoutFactory, EventFactory
from apps.fighters.tests.factories import FighterFactory
from apps.predictions.selectors import get_prediction_outcomes
from apps.predictions.services import SOURCE_WALK_FORWARD, record_prediction

VERSAO = "2026-08-31T10:00:00+00:00"
OUTRA_VERSAO = "2026-09-15T10:00:00+00:00"


def _seed_fighter(session: Session) -> int:
    fighter = FighterFactory.build()
    session.add(fighter)
    session.flush()
    return fighter.id


def _seed_bout(session: Session, *, winner_id: int | None) -> int:
    """Semeia evento + luta com o vencedor dado (``None`` = sem resultado)."""
    event = EventFactory.build(date=date(2026, 8, 1))
    session.add(event)
    session.flush()
    bout = BoutFactory.build(event_id=event.id, winner_id=winner_id)
    session.add(bout)
    session.flush()
    return bout.id


def test_predicao_correta_em_luta_decidida_marca_acerto(db_session: Session) -> None:
    """CA-08: previsto == vencedor real -> ``hit is True``, com o vencedor real exposto."""
    vencedor = _seed_fighter(db_session)
    bout_id = _seed_bout(db_session, winner_id=vencedor)
    record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=vencedor,
        prob_predicted_winner=0.66,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_WALK_FORWARD,
    )

    (outcome,) = get_prediction_outcomes(db_session, bout_id)

    assert outcome.hit is True
    assert outcome.actual_winner_id == vencedor
    assert outcome.prediction.predicted_winner_id == vencedor
    assert outcome.prediction.model_version == VERSAO


def test_predicao_errada_em_luta_decidida_marca_erro(db_session: Session) -> None:
    """CA-08: previsto != vencedor real -> ``hit is False``."""
    vencedor = _seed_fighter(db_session)
    perdedor = _seed_fighter(db_session)
    bout_id = _seed_bout(db_session, winner_id=vencedor)
    record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=perdedor,
        prob_predicted_winner=0.51,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_WALK_FORWARD,
    )

    (outcome,) = get_prediction_outcomes(db_session, bout_id)

    assert outcome.hit is False
    assert outcome.actual_winner_id == vencedor


def test_luta_sem_vencedor_nao_tem_acerto_nem_erro(db_session: Session) -> None:
    """CA-08: ``bouts.winner_id`` nulo -> ``hit is None``, nunca ``False``.

    Luta ainda sem resultado, empate ou no contest: sem gabarito não há acerto nem erro,
    e a linha fica fora do denominador de qualquer métrica calculada depois.
    """
    previsto = _seed_fighter(db_session)
    bout_id = _seed_bout(db_session, winner_id=None)
    record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=previsto,
        prob_predicted_winner=0.6,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_WALK_FORWARD,
    )

    (outcome,) = get_prediction_outcomes(db_session, bout_id)

    assert outcome.actual_winner_id is None
    assert outcome.hit is None


def test_sem_vencedor_previsto_nao_ha_acerto_a_computar(db_session: Session) -> None:
    """Predição sem vencedor previsto -> ``hit is None``, mesmo com a luta decidida."""
    vencedor = _seed_fighter(db_session)
    bout_id = _seed_bout(db_session, winner_id=vencedor)
    record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=None,
        prob_predicted_winner=0.5,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_WALK_FORWARD,
    )

    (outcome,) = get_prediction_outcomes(db_session, bout_id)

    assert outcome.actual_winner_id == vencedor
    assert outcome.hit is None


def test_duas_versoes_de_modelo_devolvem_a_serie_da_luta(db_session: Session) -> None:
    """CA-08: a série por versão de modelo é legível, em ordem determinística."""
    vencedor = _seed_fighter(db_session)
    perdedor = _seed_fighter(db_session)
    bout_id = _seed_bout(db_session, winner_id=vencedor)
    record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=perdedor,
        prob_predicted_winner=0.52,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_WALK_FORWARD,
    )
    record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=vencedor,
        prob_predicted_winner=0.64,
        model_version=OUTRA_VERSAO,
        n_features=45,
        source=SOURCE_WALK_FORWARD,
    )

    outcomes = get_prediction_outcomes(db_session, bout_id)

    assert [o.prediction.model_version for o in outcomes] == [VERSAO, OUTRA_VERSAO]
    assert [o.hit for o in outcomes] == [False, True]


def test_luta_sem_predicao_devolve_lista_vazia(db_session: Session) -> None:
    """Luta sem nenhuma predição registrada devolve lista vazia (não levanta)."""
    bout_id = _seed_bout(db_session, winner_id=None)

    assert get_prediction_outcomes(db_session, bout_id) == []
