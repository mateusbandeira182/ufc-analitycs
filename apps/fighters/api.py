"""Router fino de fighters (somente leitura).

Valida a entrada, delega a leitura ao selector e devolve o response schema; não
executa query no router. ``list`` responde o envelope ``Page[FighterOut]`` e
``detail`` levanta 404 quando o selector não encontra o lutador.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.bouts.api import bout_fighter_stats_out
from apps.fighters.schemas import (
    FighterBoutOut,
    FighterOpponentOut,
    FighterOut,
    FighterStatsOut,
)
from apps.fighters.selectors import (
    get_fighter_by_id,
    get_fighter_history,
    get_fighter_stats,
    list_fighters,
)
from mma_analytics.db import get_session
from mma_analytics.pagination import Page, PageParams, page_params

router = APIRouter(prefix="/fighters", tags=["fighters"])


@router.get("", response_model=Page[FighterOut])
def list_fighters_endpoint(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PageParams, Depends(page_params)],
    name: Annotated[str | None, Query()] = None,
) -> Page[FighterOut]:
    """Lista fighters paginados, com filtro opcional por nome."""
    rows, total = list_fighters(session, name=name, limit=params.limit, offset=params.offset)
    return Page[FighterOut](
        items=[FighterOut.model_validate(row) for row in rows],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )


@router.get("/{fighter_id}", response_model=FighterOut)
def get_fighter_endpoint(
    fighter_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> FighterOut:
    """Detalha um fighter pelo id; responde 404 quando não existe."""
    fighter = get_fighter_by_id(session, fighter_id)
    if fighter is None:
        raise HTTPException(status_code=404, detail="Fighter não encontrado")
    return FighterOut.model_validate(fighter)


@router.get("/{fighter_id}/bouts", response_model=list[FighterBoutOut])
def get_fighter_history_endpoint(
    fighter_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> list[FighterBoutOut]:
    """Histórico do lutador em ordem cronológica; responde 404 quando não existe."""
    if get_fighter_by_id(session, fighter_id) is None:
        raise HTTPException(status_code=404, detail="Fighter não encontrado")
    return [
        FighterBoutOut(
            bout_id=row.bout.id,
            event_id=row.event.id,
            event_name=row.event.name,
            event_date=row.event.date,
            method=row.bout.method,
            round=row.bout.round,
            ending_time_seconds=row.bout.ending_time_seconds,
            won=row.bout.winner_id == fighter_id,
            stats=bout_fighter_stats_out(row.stats),
            opponent=(
                FighterOpponentOut(
                    fighter_id=row.opponent.fighter_id, name=row.opponent.fighter.name
                )
                if row.opponent is not None
                else None
            ),
        )
        for row in get_fighter_history(session, fighter_id)
    ]


@router.get("/{fighter_id}/stats", response_model=FighterStatsOut)
def get_fighter_stats_endpoint(
    fighter_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> FighterStatsOut:
    """Estatísticas resumidas do lutador, computadas on demand; 404 quando não existe."""
    stats = get_fighter_stats(session, fighter_id)
    if stats is None:
        raise HTTPException(status_code=404, detail="Fighter não encontrado")
    return FighterStatsOut(
        fighter_id=stats.fighter_id,
        bouts_counted=stats.bouts_counted,
        avg_sig_strikes_landed=stats.avg_sig_strikes_landed,
        avg_takedowns_landed=stats.avg_takedowns_landed,
        avg_control_time_seconds=stats.avg_control_time_seconds,
        wins_by_method=stats.wins_by_method,
    )
