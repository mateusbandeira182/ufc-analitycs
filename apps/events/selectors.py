"""Leitura de events (sem efeito colateral).

Recebe a ``Session`` por injeção e devolve models do domínio. ``list_events``
ordena por data decrescente (mais recentes primeiro), com desempate por ``id``
decrescente para paginação determinística no Postgres. ``list_event_bouts`` faz a
leitura cross-app de ``Bout`` -- na ordem do card (``bout_order``, nulos por último) e
**sem** filtrar ``status`` -- e carrega, via ``selectinload``, os cantos de cada
luta com a identidade do lutador (``Bout.bout_fighters`` -> ``BoutFighter.fighter``)
para o card exibir os participantes -- sem o box-score granular, que fica no
detalhe da luta, e sem N+1.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from apps.bouts.models import Bout, BoutFighter
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
    """Devolve os bouts do card do event, na ordem do card (``bout_order``).

    ``bout_order`` chegou no M6 e é **ordenável, mas não sequencial nem denso** -- o payload
    real da Cito traz 1001 na luta principal --, então só a ordem relativa dentro do evento
    é confiável. Os nulos vão para o fim (``nulls_last``): luta sem contexto de card não pode
    encabeçar o card, e o desempate por ``id`` mantém a lista reprodutível.

    **Não filtra ``status``** (M9, RF-07), e isso é decisão ativa: o card de um evento futuro
    é feito inteiro de lutas agendadas, e quem decide o que exibir é a tela. É o único
    selector de leitura do M9 que enxerga luta ainda não realizada.

    Carrega os cantos (``bout_fighters``) e a identidade do lutador de cada canto via
    ``selectinload``, para o card expor os participantes sem N+1 -- apenas identidade
    (id/nome/canto), o box-score granular fica no detalhe da luta.
    """
    stmt = (
        select(Bout)
        .where(Bout.event_id == event_id)
        .options(selectinload(Bout.bout_fighters).selectinload(BoutFighter.fighter))
        .order_by(Bout.bout_order.asc().nulls_last(), Bout.id.asc())
    )
    return list(session.scalars(stmt).all())
