"""A guarda da pipeline de features contra luta agendada (SPEC 011, Slice 02).

Ponto 8 da tabela de varredura e CA-07: ``read_granular`` é o **ponto de entrada único** da
pipeline de features, e é ali que o recorte ``status='completed'`` mora. Uma luta agendada
cria duas linhas em ``bout_fighters``; sem a guarda, elas entrariam na frame longa como se
fossem histórico -- com desfecho nulo e box-score vazio --, e o dataset de treino cresceria
com dado inexistente.

A guarda é **única e na query de ``bouts``**, não repetida na de ``bout_fighters``: o merge
de ``build_long_frame`` é ``how="inner"`` em ``bout_id``, então derrubar a luta derruba os
cantos. Uma segunda guarda esconderia qual das duas de fato protege.

Toda luta agendada desta sprint é semeada **à mão**: a ingestão que as escreve de verdade é
da Slice 04, e esta slice precede obrigatoriamente aquela.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from apps.bouts.enums import BoutStatus, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.features.long_frame import build_long_frame, read_granular
from tests.features.test_long_frame import _add_bout, _add_event, _add_fighter

# Data fixa, não relativa ao relógio: o que a guarda recorta é ``status``, nunca a data --
# usar "hoje + 60 dias" sugeriria que a pipeline filtra por futuridade, o que ela não faz.
_DATA_FUTURA = date(2026, 12, 12)


def _add_scheduled_bout(session: Session, *, event: Event, red: Fighter, blue: Fighter) -> Bout:
    """Semeia uma luta **agendada**: sem desfecho, com os dois cantos e stats nulas.

    Os cantos entram de propósito -- é por ``bout_fighters`` que a luta agendada vaza para a
    pipeline, e um teste que semeasse só a linha de ``bouts`` não exerceria nada.
    """
    bout = Bout(
        event_id=event.id,
        winner_id=None,
        method=None,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        title_bout=None,
        scheduled_rounds=None,
        status=BoutStatus.SCHEDULED,
        source="cito",
    )
    session.add(bout)
    session.flush()
    for corner, fighter in ((Corner.RED, red), (Corner.BLUE, blue)):
        session.add(
            BoutFighter(
                bout_id=bout.id,
                fighter_id=fighter.id,
                corner=corner,
                source="cito",
            )
        )
    session.flush()
    return bout


def _cenario(session: Session) -> tuple[Event, Fighter, Fighter]:
    """Um evento passado com uma luta realizada entre dois lutadores."""
    red = _add_fighter(session, "Fighter Alpha")
    blue = _add_fighter(session, "Fighter Bravo")
    event = _add_event(session, "UFC Passado", date(2019, 5, 4))
    _add_bout(session, event=event, red=red, blue=blue, winner=red, red_sig_strikes=40)
    return event, red, blue


def test_dataset_nao_muda_com_luta_agendada_no_banco(db_session: Session) -> None:
    """CA-07: a frame longa tem a **mesma contagem e as mesmas colunas** com e sem a agendada.

    É o critério que a SPEC pede literalmente -- rodar a pipeline com uma luta ``scheduled``
    no banco produz o dataset que produzia sem ela.
    """
    _, red, blue = _cenario(db_session)
    antes = build_long_frame(read_granular(db_session))
    linhas_antes, colunas_antes = len(antes), list(antes.columns)

    futuro = _add_event(db_session, "UFC Card Futuro", _DATA_FUTURA)
    _add_scheduled_bout(db_session, event=futuro, red=red, blue=blue)
    depois = build_long_frame(read_granular(db_session))

    assert len(depois) == linhas_antes
    assert list(depois.columns) == colunas_antes


def test_read_granular_nao_traz_a_luta_agendada(db_session: Session) -> None:
    """Ponto 8: a guarda é a query de ``bouts``, não um acidente do merge.

    Sem este teste, trocar o ``how="inner"`` de ``build_long_frame`` por um ``left`` faria a
    agendada reaparecer sem que nada acusasse -- o teste de contagem acima passaria a
    depender de um detalhe de outra função.
    """
    _, red, blue = _cenario(db_session)
    futuro = _add_event(db_session, "UFC Card Futuro", _DATA_FUTURA)
    agendada = _add_scheduled_bout(db_session, event=futuro, red=red, blue=blue)

    frames = read_granular(db_session)

    assert agendada.id not in set(frames.bouts["bout_id"])
    assert len(frames.bouts) == 1


def test_a_luta_agendada_existe_no_banco_com_os_dois_cantos(db_session: Session) -> None:
    """Guarda de não-vacuidade: a agendada **está** persistida, com as duas linhas de canto.

    Sem esta asserção, os dois testes acima passariam mesmo que a semeadura estivesse
    quebrada -- e a guarda que eles existem para provar nunca teria sido exercida.
    """
    _, red, blue = _cenario(db_session)
    futuro = _add_event(db_session, "UFC Card Futuro", _DATA_FUTURA)
    agendada = _add_scheduled_bout(db_session, event=futuro, red=red, blue=blue)

    persistida = db_session.get(Bout, agendada.id)

    assert persistida is not None
    assert persistida.status is BoutStatus.SCHEDULED
    assert persistida.method is None
    assert len(persistida.bout_fighters) == 2
