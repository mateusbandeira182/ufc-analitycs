"""Testes da gravação de predições (Service) contra o Postgres de teste transacional.

Cobrem o que a Slice 07 existe para garantir (CA-08 da SPEC 007): a gravação é
**idempotente na chave natural** ``(bout_id, model_version)`` -- prever a mesma luta com
o mesmo modelo atualiza a linha existente em vez de duplicar -- e uma versão de modelo
diferente gera linha nova, que é o que faz a série temporal do walk-forward existir.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.tests.factories import BoutFactory, EventFactory
from apps.fighters.tests.factories import FighterFactory
from apps.predictions.models import BoutPrediction
from apps.predictions.services import SOURCE_API, SOURCE_WALK_FORWARD, record_prediction

VERSAO = "2026-08-31T10:00:00+00:00"
OUTRA_VERSAO = "2026-09-15T10:00:00+00:00"


def _seed_bout(session: Session, *, winner_id: int | None = None) -> int:
    """Semeia evento + luta e devolve o ``bout_id`` persistido."""
    event = EventFactory.build(date=date(2026, 8, 1))
    session.add(event)
    session.flush()
    bout = BoutFactory.build(event_id=event.id, winner_id=winner_id)
    session.add(bout)
    session.flush()
    return bout.id


def _seed_fighter(session: Session) -> int:
    """Semeia um lutador e devolve o ``fighter_id`` persistido."""
    fighter = FighterFactory.build()
    session.add(fighter)
    session.flush()
    return fighter.id


def _contagem(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(BoutPrediction)) or 0


def test_record_prediction_grava_a_predicao(db_session: Session) -> None:
    """A gravação persiste vencedor previsto, probabilidade, versão e contagem de features."""
    bout_id = _seed_bout(db_session)
    fighter_id = _seed_fighter(db_session)

    prediction_id = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=fighter_id,
        prob_predicted_winner=0.62,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_API,
    )

    gravada = db_session.get(BoutPrediction, prediction_id)
    assert gravada is not None
    assert gravada.bout_id == bout_id
    assert gravada.predicted_winner_id == fighter_id
    assert gravada.prob_predicted_winner == 0.62
    assert gravada.model_version == VERSAO
    assert gravada.n_features == 39


def test_record_prediction_e_idempotente_na_chave_natural(db_session: Session) -> None:
    """CA-08: gravar 2x a mesma ``(bout_id, model_version)`` mantém **uma** linha.

    O id devolvido é o mesmo (é a mesma linha, não uma nova) e o conteúdo reflete a
    última afirmação daquele modelo sobre aquela luta.
    """
    bout_id = _seed_bout(db_session)
    primeiro_previsto = _seed_fighter(db_session)
    segundo_previsto = _seed_fighter(db_session)

    primeiro = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=primeiro_previsto,
        prob_predicted_winner=0.55,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_API,
    )
    segundo = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=segundo_previsto,
        prob_predicted_winner=0.71,
        model_version=VERSAO,
        n_features=42,
        source=SOURCE_WALK_FORWARD,
    )

    assert primeiro == segundo
    assert _contagem(db_session) == 1
    atualizada = db_session.get(BoutPrediction, segundo)
    assert atualizada is not None
    db_session.refresh(atualizada)
    assert atualizada.predicted_winner_id == segundo_previsto
    assert atualizada.prob_predicted_winner == 0.71
    assert atualizada.n_features == 42
    assert atualizada.source == SOURCE_WALK_FORWARD


def test_record_prediction_versao_nova_gera_linha_nova(db_session: Session) -> None:
    """CA-08: a mesma luta com ``model_version`` diferente acumula linhas.

    É essa acumulação que forma a série temporal por versão de modelo -- a curva de
    refino do walk-forward existe como dado por causa dela.
    """
    bout_id = _seed_bout(db_session)
    fighter_id = _seed_fighter(db_session)

    primeiro = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=fighter_id,
        prob_predicted_winner=0.55,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_WALK_FORWARD,
    )
    segundo = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=fighter_id,
        prob_predicted_winner=0.58,
        model_version=OUTRA_VERSAO,
        n_features=45,
        source=SOURCE_WALK_FORWARD,
    )

    assert primeiro != segundo
    assert _contagem(db_session) == 2


def test_record_prediction_carimba_source_e_instante_tz_aware(db_session: Session) -> None:
    """Rastreio: ``source`` é gravado como passado e ``predicted_at`` é tz-aware em UTC."""
    bout_id = _seed_bout(db_session)
    fighter_id = _seed_fighter(db_session)
    antes = datetime.now(UTC)

    prediction_id = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=fighter_id,
        prob_predicted_winner=0.5,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_WALK_FORWARD,
    )

    gravada = db_session.get(BoutPrediction, prediction_id)
    assert gravada is not None
    assert gravada.source == SOURCE_WALK_FORWARD
    assert gravada.predicted_at.tzinfo is not None
    assert antes <= gravada.predicted_at <= datetime.now(UTC)


def test_record_prediction_aceita_vencedor_previsto_nulo(db_session: Session) -> None:
    """Sem vencedor previsto a linha ainda é gravável (simetria com ``bouts.winner_id``)."""
    bout_id = _seed_bout(db_session)

    prediction_id = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=None,
        prob_predicted_winner=0.5,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_API,
    )

    gravada = db_session.get(BoutPrediction, prediction_id)
    assert gravada is not None
    assert gravada.predicted_winner_id is None
