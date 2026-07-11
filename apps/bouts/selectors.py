"""Leitura de bouts (sem efeito colateral).

``get_bout_by_id`` compõe a luta com o seu evento e os dois cantos de
``bout_fighters``, trazendo as stats granulares de cada lutador **como foram
gravadas** -- nunca médias agregadas (invariante do CLAUDE.md, ADR 0001). A
leitura é explícita (sem ``relationship()`` nos models); os cantos saem em ordem
determinística por ``corner`` para o response ser estável.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event


@dataclass(frozen=True)
class BoutDetail:
    """Composição de leitura de uma luta: a luta, o evento e os dois cantos."""

    bout: Bout
    event: Event
    fighters: list[BoutFighter]  # ordem determinística por corner


def _load_corners(session: Session, bout_id: int) -> list[BoutFighter]:
    """Carrega os dois cantos de uma luta em ordem determinística por ``corner``."""
    return list(
        session.scalars(
            select(BoutFighter).where(BoutFighter.bout_id == bout_id).order_by(BoutFighter.corner)
        )
    )


def _load_corners_batch(session: Session, bout_ids: Sequence[int]) -> dict[int, list[BoutFighter]]:
    """Carrega os cantos de vários bouts em UMA query, agrupados por ``bout_id``.

    Evita o N+1 de consultar canto a canto: uma única ``SELECT ... WHERE bout_id
    IN (...)`` ordenada por ``bout_id`` e ``corner`` alimenta o agrupamento em
    memória, preservando a mesma ordem determinística por ``corner`` dentro de
    cada luta que ``_load_corners`` garante para o caso singular.
    """
    if not bout_ids:
        return {}
    corners_por_bout: dict[int, list[BoutFighter]] = defaultdict(list)
    corners = session.scalars(
        select(BoutFighter)
        .where(BoutFighter.bout_id.in_(bout_ids))
        .order_by(BoutFighter.bout_id, BoutFighter.corner)
    )
    for bout_fighter in corners:
        corners_por_bout[bout_fighter.bout_id].append(bout_fighter)
    return corners_por_bout


def get_bout_by_id(session: Session, bout_id: int) -> BoutDetail | None:
    """Devolve a composição da luta pelo id, ou ``None`` quando não existe."""
    bout = session.get(Bout, bout_id)
    if bout is None:
        return None
    # ``event_id`` é FK NOT NULL: o evento sempre existe. O guard mantém o tipo
    # de ``BoutDetail.event`` como ``Event`` (não ``Event | None``) sem assert.
    event = session.get(Event, bout.event_id)
    if event is None:
        return None
    return BoutDetail(bout=bout, event=event, fighters=_load_corners(session, bout_id))


def get_head_to_head(session: Session, a_id: int, b_id: int) -> list[BoutDetail]:
    """Devolve os confrontos diretos entre dois lutadores em ordem cronológica.

    Intersecta os bouts em que **ambos** os ``fighter_id`` aparecem em
    ``bout_fighters`` via dois aliases sobre o mesmo ``bout_id`` -- sem ``OR`` nos
    dois cantos, o ganho direto do schema long (ADR 0001). O join em ``events`` só
    ordena por ``date`` ascendente (desempate por ``bouts.id`` para lutas na mesma
    data). Cada bout carrega os dois cantos com as stats granulares como gravadas.
    Não valida existência dos lutadores (responsabilidade do router): dois
    lutadores sem confronto direto devolvem lista vazia. Os cantos de todos os
    bouts do resultado são carregados numa única query em lote (sem N+1).
    """
    bf_a = aliased(BoutFighter)
    bf_b = aliased(BoutFighter)
    stmt = (
        select(Bout, Event)
        .join(bf_a, bf_a.bout_id == Bout.id)
        .join(bf_b, bf_b.bout_id == Bout.id)
        .join(Event, Event.id == Bout.event_id)
        .where(bf_a.fighter_id == a_id, bf_b.fighter_id == b_id)
        .order_by(Event.date.asc(), Bout.id.asc())
    )
    rows = session.execute(stmt).tuples().all()
    corners_por_bout = _load_corners_batch(session, [bout.id for bout, _ in rows])
    return [
        BoutDetail(bout=bout, event=event, fighters=corners_por_bout.get(bout.id, []))
        for bout, event in rows
    ]
