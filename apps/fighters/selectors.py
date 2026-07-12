"""Leitura de fighters (sem efeito colateral).

Recebe a ``Session`` por injeção e devolve models do domínio. ``list_fighters``
retorna a página de linhas e o total do conjunto filtrado; a ordenação por
``name`` é obrigatória para o ``limit``/``offset`` paginar de forma determinística
no Postgres. O filtro por nome é ``ILIKE '%name%'`` (case-insensitive substring).

``get_fighter_history`` monta a série temporal do lutador com join explícito
(``bout_fighters`` -> ``bouts`` -> ``events``). Cada linha traz as stats granulares
**do canto consultado** naquela luta -- nunca do oponente, nunca médias (ADR 0001) --
e o adversário daquela luta (o outro canto), resolvido em lote para o card da SPA.
A identidade dos lutadores (própria e do adversário) entra via
``selectinload(BoutFighter.fighter)`` (relationship só-leitura, sem migration).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from apps.bouts.enums import BoutMethod
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter


def list_fighters(
    session: Session, *, name: str | None, limit: int, offset: int
) -> tuple[list[Fighter], int]:
    """Devolve ``(linhas, total)`` de fighters, opcionalmente filtrados por nome.

    ``total`` é a contagem do conjunto após o filtro (não da página), para o
    envelope de paginação. Um ``name`` vazio ou ausente não filtra.
    """
    stmt = select(Fighter)
    count_stmt = select(func.count()).select_from(Fighter)
    if name:
        name_filter = Fighter.name.ilike(f"%{name}%")
        stmt = stmt.where(name_filter)
        count_stmt = count_stmt.where(name_filter)

    total = session.scalar(count_stmt) or 0
    rows = session.scalars(stmt.order_by(Fighter.name).limit(limit).offset(offset)).all()
    return list(rows), total


def get_fighter_by_id(session: Session, fighter_id: int) -> Fighter | None:
    """Devolve o fighter pelo id, ou ``None`` quando não existe."""
    return session.get(Fighter, fighter_id)


@dataclass(frozen=True)
class FighterBoutRow:
    """Uma luta do histórico do lutador: o canto consultado, a luta e o evento.

    ``opponent`` é o outro canto daquela luta (o ``bout_fighter`` cujo
    ``fighter_id`` difere do consultado). É ``None`` só em dados sujos -- luta sem
    o segundo canto gravado; no caso normal há exatamente um adversário.
    """

    stats: BoutFighter  # linha de ``bout_fighters`` do lutador consultado
    bout: Bout
    event: Event
    opponent: BoutFighter | None  # o outro canto (adversário) daquela luta


def _load_opponents(
    session: Session, bout_ids: Sequence[int], fighter_id: int
) -> dict[int, BoutFighter]:
    """Resolve o adversário (o outro canto) de cada luta, em UMA query em lote.

    Carrega os ``bout_fighters`` das lutas cujo ``fighter_id`` difere do consultado,
    com a identidade do lutador (``selectinload``), agrupando por ``bout_id``. No
    caso normal há um único adversário por luta; se dados sujos trouxerem mais de
    um canto extra, mantém o primeiro pela ordem determinística de ``corner``.
    """
    if not bout_ids:
        return {}
    corners = session.scalars(
        select(BoutFighter)
        .where(BoutFighter.bout_id.in_(bout_ids), BoutFighter.fighter_id != fighter_id)
        .options(selectinload(BoutFighter.fighter))
        .order_by(BoutFighter.bout_id, BoutFighter.corner)
    )
    opponents: dict[int, BoutFighter] = {}
    for corner in corners:
        opponents.setdefault(corner.bout_id, corner)
    return opponents


def get_fighter_history(session: Session, fighter_id: int) -> list[FighterBoutRow]:
    """Devolve o histórico do lutador em ordem cronológica ascendente.

    Join explícito ``bout_fighters`` -> ``bouts`` -> ``events`` filtrado pelo
    ``fighter_id``, ordenado por ``events.date`` ascendente com desempate
    determinístico por ``bouts.id`` (lutas na mesma data). Cada item carrega as
    stats granulares daquele lutador naquela luta e o adversário (outro canto),
    ambos com a identidade do lutador eager-loaded. Lutador sem lutas devolve
    lista vazia (o not-found é decidido no router).
    """
    stmt = (
        select(BoutFighter, Bout, Event)
        .join(Bout, BoutFighter.bout_id == Bout.id)
        .join(Event, Bout.event_id == Event.id)
        .where(BoutFighter.fighter_id == fighter_id)
        .options(selectinload(BoutFighter.fighter))
        .order_by(Event.date.asc(), Bout.id.asc())
    )
    rows = session.execute(stmt).tuples().all()
    opponents = _load_opponents(session, [bout.id for _, bout, _ in rows], fighter_id)
    return [
        FighterBoutRow(stats=stats, bout=bout, event=event, opponent=opponents.get(bout.id))
        for stats, bout, event in rows
    ]


@dataclass(frozen=True)
class FighterStats:
    """Resumo estatístico do lutador computado on demand a partir de ``bout_fighters``.

    As médias são ``None`` quando não há valor não-nulo a agregar (lutador sem
    lutas, ou stat sempre nula). ``wins_by_method`` mapeia o valor do método
    (ex.: ``"ko_tko"``) para a contagem de vitórias -- só entram as lutas em que
    ``winner_id`` é o próprio lutador (empate/no contest nunca contam).
    """

    fighter_id: int
    bouts_counted: int
    avg_sig_strikes_landed: float | None
    avg_takedowns_landed: float | None
    avg_control_time_seconds: float | None
    wins_by_method: dict[str, int]


def get_fighter_stats(session: Session, fighter_id: int) -> FighterStats | None:
    """Devolve as estatísticas resumidas do lutador, ou ``None`` se ele não existe.

    Agregação **sempre on demand** (invariante RF-08 / CLAUDE.md): nada é
    persistido. A checagem de existência via ``get_fighter_by_id`` distingue
    "lutador inexistente" (``None`` -> 404 no router) de "lutador sem lutas"
    (``bouts_counted == 0``, médias ``None``, ``wins_by_method`` vazio). No
    Postgres ``func.avg`` retorna ``Decimal | None`` e ignora NULL -- a fronteira
    converte para ``float | None``.
    """
    if get_fighter_by_id(session, fighter_id) is None:
        return None

    aggregate = session.execute(
        select(
            func.avg(BoutFighter.sig_strikes_landed),
            func.avg(BoutFighter.takedowns_landed),
            func.avg(BoutFighter.control_time_seconds),
            func.count(BoutFighter.id),
        ).where(BoutFighter.fighter_id == fighter_id)
    ).one()

    wins_by_method: dict[str, int] = {}
    wins_rows = session.execute(
        select(Bout.method, func.count(Bout.id))
        .where(Bout.winner_id == fighter_id)
        .group_by(Bout.method)
    ).all()
    for method, count in wins_rows:
        won_method: BoutMethod = method
        wins_by_method[won_method.value] = int(count)

    return FighterStats(
        fighter_id=fighter_id,
        bouts_counted=int(aggregate[3]),
        avg_sig_strikes_landed=float(aggregate[0]) if aggregate[0] is not None else None,
        avg_takedowns_landed=float(aggregate[1]) if aggregate[1] is not None else None,
        avg_control_time_seconds=float(aggregate[2]) if aggregate[2] is not None else None,
        wins_by_method=wins_by_method,
    )
