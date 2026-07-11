"""Leitura de events (sem efeito colateral).

Recebe a ``Session`` por injeção e devolve models do domínio. ``list_events``
ordena por data decrescente (mais recentes primeiro), com desempate por ``id``
decrescente para paginação determinística no Postgres. ``list_event_bouts`` faz a
leitura cross-app de ``Bout`` (só o card do event; as stats granulares de
``bout_fighters`` ficam na Slice 03).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.models import Bout
from apps.events.models import Event


def list_events(session: Session, *, limit: int, offset: int) -> tuple[list[Event], int]:
    """Devolve ``(linhas, total)`` de events por data desc (mais recentes primeiro).

    ``total`` é a contagem geral (não da página), para o envelope de paginação. O
    desempate por ``id`` desc mantém a janela reprodutível quando há datas iguais.
    """
    total = session.scalar(select(func.count()).select_from(Event)) or 0
    stmt = select(Event).order_by(Event.date.desc(), Event.id.desc()).limit(limit).offset(offset)
    rows = session.scalars(stmt).all()
    return list(rows), total


def get_event_by_id(session: Session, event_id: int) -> Event | None:
    """Devolve o event pelo id, ou ``None`` quando não existe."""
    return session.get(Event, event_id)


def list_event_bouts(session: Session, event_id: int) -> list[Bout]:
    """Devolve os bouts do card do event, em ordem determinística por ``id``.

    O schema M0 não tem coluna de posição no card; ordenar por ``id`` é estável e
    suficiente. Não faz join em ``bout_fighters`` -- stats granulares na Slice 03.
    """
    stmt = select(Bout).where(Bout.event_id == event_id).order_by(Bout.id)
    return list(session.scalars(stmt).all())
