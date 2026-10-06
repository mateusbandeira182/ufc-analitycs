"""Testes de Selector de events contra o Postgres de teste transacional.

Cobrem ``list_events`` (ordem por data decrescente, paginação por limit/offset,
janela vazia), ``get_event_by_id`` (encontrado e inexistente) e ``list_event_bouts``
(bouts do card, event sem bouts e -- no M9, ponto 2 da varredura de ``bouts.status`` --
a luta agendada, que **entra** no card, com a ordenação por ``bout_order``). Semeiam via
factories e exercitam o selector direto, sem subir a API.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, BoutStatus, Corner
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.events.models import Event
from apps.events.selectors import get_event_by_id, list_event_bouts, list_events
from apps.fighters.tests.factories import FighterFactory


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


def test_list_event_bouts_carrega_os_cantos_com_identidade(db_session: Session) -> None:
    """Cada bout do card carrega os dois cantos com a identidade do lutador (enrich SPA)."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    red = FighterFactory.build(name="Alex Pereira")
    blue = FighterFactory.build(name="Jamahal Hill")
    db_session.add_all([red, blue])
    db_session.flush()
    bout = BoutFactory.build(event_id=event.id, weight_class="Light Heavyweight")
    db_session.add(bout)
    db_session.flush()
    db_session.add_all(
        [
            BoutFighterFactory.build(bout_id=bout.id, fighter_id=red.id, corner=Corner.RED),
            BoutFighterFactory.build(bout_id=bout.id, fighter_id=blue.id, corner=Corner.BLUE),
        ]
    )
    db_session.flush()

    bouts = list_event_bouts(db_session, event.id)

    assert len(bouts) == 1
    por_canto = {bf.corner: bf for bf in bouts[0].bout_fighters}
    assert set(por_canto) == {Corner.RED, Corner.BLUE}
    assert por_canto[Corner.RED].fighter_id == red.id
    assert por_canto[Corner.RED].fighter.name == "Alex Pereira"
    assert por_canto[Corner.BLUE].fighter.name == "Jamahal Hill"


def test_list_event_bouts_event_sem_bouts_devolve_vazio(db_session: Session) -> None:
    """Event sem bouts devolve lista vazia."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))

    assert list_event_bouts(db_session, event.id) == []


def test_list_event_bouts_inclui_a_luta_agendada(db_session: Session) -> None:
    """Ponto 2 da varredura (M9, RF-07): o card **não** filtra ``status``.

    Decisão ativa, não omissão: o card de um evento futuro é feito inteiro de lutas
    agendadas, e quem decide o que exibir é a tela. Filtrar aqui esvaziaria a página do
    evento por vir -- exatamente a tela que o M9 existe para entregar.
    """
    event = _add_event(db_session, "UFC Card Futuro", date(2026, 12, 12))
    realizada = BoutFactory.build(event_id=event.id, weight_class="Lightweight")
    agendada = BoutFactory.build(
        event_id=event.id,
        winner_id=None,
        method=None,
        status=BoutStatus.SCHEDULED,
        weight_class="Welterweight",
    )
    db_session.add_all([realizada, agendada])
    db_session.flush()

    rows = list_event_bouts(db_session, event.id)

    assert {bout.id for bout in rows} == {realizada.id, agendada.id}
    assert {bout.status for bout in rows} == {BoutStatus.COMPLETED, BoutStatus.SCHEDULED}


def test_list_event_bouts_ordena_por_bout_order_com_nulos_no_fim(db_session: Session) -> None:
    """Ponto 2 da varredura: a ordem do card é ``bout_order asc nulls_last, id asc``.

    ``bout_order`` é **ordenável, mas não sequencial nem denso** -- o payload real da Cito
    traz 1001 na luta principal --, então o teste afirma a ordem relativa, nunca os valores.
    Os nulos vão para o fim porque luta sem contexto de card não pode encabeçar o card, e o
    desempate por ``id`` mantém a lista reprodutível entre execuções.

    As lutas são inseridas **fora** da ordem de ``id`` de propósito: com elas em ordem, o
    ``order_by(Bout.id)`` antigo passaria por coincidência.
    """
    event = _add_event(db_session, "UFC Card Futuro", date(2026, 12, 12))
    sem_ordem = BoutFactory.build(event_id=event.id, bout_order=None, weight_class="Flyweight")
    db_session.add(sem_ordem)
    db_session.flush()
    principal = BoutFactory.build(
        event_id=event.id,
        winner_id=None,
        method=None,
        status=BoutStatus.SCHEDULED,
        bout_order=1001,
        weight_class="Welterweight",
    )
    db_session.add(principal)
    db_session.flush()
    coprincipal = BoutFactory.build(
        event_id=event.id,
        method=BoutMethod.DECISION,
        bout_order=1002,
        weight_class="Lightweight",
    )
    db_session.add(coprincipal)
    db_session.flush()

    rows = list_event_bouts(db_session, event.id)

    assert [bout.id for bout in rows] == [principal.id, coprincipal.id, sem_ordem.id]
