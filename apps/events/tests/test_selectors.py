"""Testes de Selector de events contra o Postgres de teste transacional.

Cobrem ``list_events`` (ordem por data decrescente, paginação por limit/offset,
janela vazia), ``get_event_by_id`` (encontrado e inexistente) e ``list_event_bouts``
(bouts do card e event sem bouts). Semeiam via factories e exercitam o selector
direto, sem subir a API.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from apps.bouts.tests.factories import BoutFactory, EventFactory
from apps.events.models import Event
from apps.events.selectors import get_event_by_id, list_event_bouts, list_events


def _add_event(session: Session, name: str, event_date: date) -> Event:
    """Semeia um event com ``name``/``date`` dados e devolve o model persistido."""
    event = EventFactory.build(name=name, date=event_date)
    session.add(event)
    session.flush()
    return event


def test_list_events_ordena_por_data_desc(db_session: Session) -> None:
    """Devolve os events mais recentes primeiro (data decrescente)."""
    _add_event(db_session, "UFC 300", date(2024, 4, 13))
    _add_event(db_session, "UFC 290", date(2023, 7, 8))
    _add_event(db_session, "UFC 310", date(2024, 12, 7))

    rows, total = list_events(db_session, limit=50, offset=0)

    assert total == 3
    assert [row.name for row in rows] == ["UFC 310", "UFC 300", "UFC 290"]


def test_list_events_pagina_com_limit_e_offset(db_session: Session) -> None:
    """``limit``/``offset`` paginam sobre a ordem recentes-primeiro."""
    _add_event(db_session, "UFC 290", date(2023, 7, 8))
    _add_event(db_session, "UFC 300", date(2024, 4, 13))
    _add_event(db_session, "UFC 310", date(2024, 12, 7))

    primeira, total = list_events(db_session, limit=2, offset=0)
    segunda, _ = list_events(db_session, limit=2, offset=2)

    assert total == 3
    assert [row.name for row in primeira] == ["UFC 310", "UFC 300"]
    assert [row.name for row in segunda] == ["UFC 290"]


def test_list_events_offset_alem_do_total_devolve_vazio(db_session: Session) -> None:
    """``offset`` além do total devolve página vazia, mas ``total`` cheio."""
    _add_event(db_session, "UFC 300", date(2024, 4, 13))
    _add_event(db_session, "UFC 310", date(2024, 12, 7))

    rows, total = list_events(db_session, limit=50, offset=10)

    assert rows == []
    assert total == 2


def test_list_events_lista_vazia(db_session: Session) -> None:
    """Sem events semeados, devolve página vazia e ``total`` zero."""
    rows, total = list_events(db_session, limit=50, offset=0)

    assert rows == []
    assert total == 0


def test_get_event_by_id_encontrado_e_inexistente(db_session: Session) -> None:
    """Devolve o event pelo id e ``None`` quando o id não existe."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))

    assert get_event_by_id(db_session, event.id) is event
    assert get_event_by_id(db_session, 999_999) is None


def test_list_event_bouts_devolve_bouts_do_event(db_session: Session) -> None:
    """Devolve os bouts do event semeado, isolando os de outros events."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    outro = _add_event(db_session, "UFC 290", date(2023, 7, 8))
    primeiro = BoutFactory.build(event_id=event.id, weight_class="Lightweight")
    segundo = BoutFactory.build(event_id=event.id, weight_class="Welterweight")
    de_outro = BoutFactory.build(event_id=outro.id, weight_class="Featherweight")
    db_session.add_all([primeiro, segundo, de_outro])
    db_session.flush()

    bouts = list_event_bouts(db_session, event.id)

    assert {bout.id for bout in bouts} == {primeiro.id, segundo.id}


def test_list_event_bouts_event_sem_bouts_devolve_vazio(db_session: Session) -> None:
    """Event sem bouts devolve lista vazia."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))

    assert list_event_bouts(db_session, event.id) == []
