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
from apps.features.schemas import CurrentFormOut, FeaturesFreshnessOut
from apps.features.selectors import get_features_freshness, read_current_form
from apps.fighters.schemas import (
    FighterBoutOut,
    FighterOpponentOut,
    FighterOut,
    FighterStatsOut,
    StrikingProfileOut,
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
    q: Annotated[
        str | None,
        Query(description="Busca por nome ou apelido; o termo vale como texto literal."),
    ] = None,
) -> Page[FighterOut]:
    """Lista fighters paginados, com busca opcional por nome ou apelido."""
    rows, total = list_fighters(session, q=q, limit=params.limit, offset=params.offset)
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
    """Estatísticas resumidas do lutador, computadas on demand; 404 quando não existe.

    Carrega ``features_freshness`` (SPEC 011, RF-05/D-09): é a resposta que alimenta a "forma
    atual" da página do lutador, uma das três que leem o cache as-of. O agregado em si **não**
    depende do cache -- é computado on demand do granular --, então a resposta continua 200
    mesmo com o cache nunca materializado; nesse caso ``materialized_at`` volta nulo.

    ``current_form`` (Slice 06, RF-15) é o bloco as-of lido de ``fighter_asof_features`` -- a
    mesma tabela que alimenta a predição. Vem **nulo**, com 200, quando o lutador não tem
    linha no cache: ausência declarada, nunca um vetor de zeros, porque zero em golpe
    significativo é uma afirmação sobre o lutador. Quem explica o motivo da ausência
    (estreante ou cache nunca materializado) é o ``features_freshness`` da mesma resposta.
    """
    stats = get_fighter_stats(session, fighter_id)
    if stats is None:
        raise HTTPException(status_code=404, detail="Fighter não encontrado")
    current_form = read_current_form(session, fighter_id)
    return FighterStatsOut(
        fighter_id=stats.fighter_id,
        bouts_counted=stats.bouts_counted,
        avg_sig_strikes_landed=stats.avg_sig_strikes_landed,
        avg_takedowns_landed=stats.avg_takedowns_landed,
        avg_control_time_seconds=stats.avg_control_time_seconds,
        wins_by_method=stats.wins_by_method,
        striking_profile=StrikingProfileOut(
            share_head=stats.striking_profile.share_head,
            share_body=stats.striking_profile.share_body,
            share_leg=stats.striking_profile.share_leg,
            share_distance=stats.striking_profile.share_distance,
            share_clinch=stats.striking_profile.share_clinch,
            share_ground=stats.striking_profile.share_ground,
        ),
        current_form=(
            CurrentFormOut.model_validate(current_form) if current_form is not None else None
        ),
        features_freshness=FeaturesFreshnessOut.model_validate(get_features_freshness(session)),
    )
