"""Routers finos de bouts e do cruzamento head-to-head (somente leitura).

Validam a entrada, delegam a leitura ao selector e montam o response schema; não
executam query no router. ``get_bout`` levanta 404 quando o selector não encontra
a luta. ``head_to_head`` serve o cruzamento ``/api/v1/head-to-head`` -- por não
pertencer ao recurso ``/bouts`` (prefixo do router de detalhe), vive num router
próprio sem prefixo, montado à parte no agregador ``/api/v1``. As stats por canto
vêm de ``bout_fighters`` (granulares, nunca médias).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.bouts.models import BoutFighter
from apps.bouts.schemas import (
    BoutDetailOut,
    BoutEventOut,
    BoutFighterRoundOut,
    BoutFighterStatsOut,
    HeadToHeadOut,
)
from apps.bouts.selectors import BoutDetail, BoutFighterRoundRow, get_bout_by_id, get_head_to_head
from apps.fighters.selectors import get_fighter_by_id
from mma_analytics.db import get_session

router = APIRouter(prefix="/bouts", tags=["bouts"])
head_to_head_router = APIRouter(tags=["head-to-head"])


def bout_fighter_stats_out(bf: BoutFighter) -> BoutFighterStatsOut:
    """Monta as stats de um canto incluindo o nome do lutador (``bf.fighter``).

    O nome vem do relationship ``BoutFighter.fighter`` (eager-loaded no selector);
    por isso a construção é explícita, em vez de ``model_validate`` sobre a linha.
    Reusado pelo histórico do lutador (``apps.fighters.api``).
    """
    return BoutFighterStatsOut(
        fighter_id=bf.fighter_id,
        name=bf.fighter.name,
        corner=bf.corner,
        knockdowns=bf.knockdowns,
        sig_strikes_landed=bf.sig_strikes_landed,
        sig_strikes_attempted=bf.sig_strikes_attempted,
        takedowns_landed=bf.takedowns_landed,
        takedowns_attempted=bf.takedowns_attempted,
        submission_attempts=bf.submission_attempts,
        control_time_seconds=bf.control_time_seconds,
        total_strikes_landed=bf.total_strikes_landed,
        total_strikes_attempted=bf.total_strikes_attempted,
        head_landed=bf.head_landed,
        head_attempted=bf.head_attempted,
        body_landed=bf.body_landed,
        body_attempted=bf.body_attempted,
        leg_landed=bf.leg_landed,
        leg_attempted=bf.leg_attempted,
        distance_landed=bf.distance_landed,
        distance_attempted=bf.distance_attempted,
        clinch_landed=bf.clinch_landed,
        clinch_attempted=bf.clinch_attempted,
        ground_landed=bf.ground_landed,
        ground_attempted=bf.ground_attempted,
        reversals=bf.reversals,
        source=bf.source,
    )


def bout_fighter_round_out(row: BoutFighterRoundRow) -> BoutFighterRoundOut:
    """Monta o schema de um round incluindo o canto e o lutador dono (``row``).

    O ``fighter_id`` e o ``corner`` vêm do ``bout_fighter`` dono do round (resolvido
    no selector); as stats vêm da linha de ``bout_fighter_rounds`` (``row.round``).
    """
    r = row.round
    return BoutFighterRoundOut(
        fighter_id=row.fighter_id,
        corner=row.corner,
        round=r.round,
        knockdowns=r.knockdowns,
        sig_strikes_landed=r.sig_strikes_landed,
        sig_strikes_attempted=r.sig_strikes_attempted,
        takedowns_landed=r.takedowns_landed,
        takedowns_attempted=r.takedowns_attempted,
        submission_attempts=r.submission_attempts,
        control_time_seconds=r.control_time_seconds,
        total_strikes_landed=r.total_strikes_landed,
        total_strikes_attempted=r.total_strikes_attempted,
        head_landed=r.head_landed,
        head_attempted=r.head_attempted,
        body_landed=r.body_landed,
        body_attempted=r.body_attempted,
        leg_landed=r.leg_landed,
        leg_attempted=r.leg_attempted,
        distance_landed=r.distance_landed,
        distance_attempted=r.distance_attempted,
        clinch_landed=r.clinch_landed,
        clinch_attempted=r.clinch_attempted,
        ground_landed=r.ground_landed,
        ground_attempted=r.ground_attempted,
        reversals=r.reversals,
        source=r.source,
    )


def _to_bout_detail_out(detail: BoutDetail) -> BoutDetailOut:
    """Monta o response schema de uma luta a partir da composição do selector."""
    return BoutDetailOut(
        id=detail.bout.id,
        event=BoutEventOut.model_validate(detail.event),
        winner_id=detail.bout.winner_id,
        method=detail.bout.method,
        round=detail.bout.round,
        ending_time_seconds=detail.bout.ending_time_seconds,
        weight_class=detail.bout.weight_class,
        title_bout=detail.bout.title_bout,
        scheduled_rounds=detail.bout.scheduled_rounds,
        referee=detail.bout.referee,
        source=detail.bout.source,
        fighters=[bout_fighter_stats_out(bf) for bf in detail.fighters],
        rounds=[bout_fighter_round_out(row) for row in detail.rounds],
    )


@router.get("/{bout_id}", response_model=BoutDetailOut)
def get_bout(
    bout_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> BoutDetailOut:
    """Detalha uma luta com evento e as stats dos dois cantos; 404 se não existe."""
    detail = get_bout_by_id(session, bout_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Bout não encontrado")
    return _to_bout_detail_out(detail)


@head_to_head_router.get("/head-to-head", response_model=HeadToHeadOut)
def head_to_head(
    session: Annotated[Session, Depends(get_session)],
    a: Annotated[int, Query(description="Id do primeiro lutador")],
    b: Annotated[int, Query(description="Id do segundo lutador")],
) -> HeadToHeadOut:
    """Confrontos diretos entre dois lutadores em ordem cronológica.

    Valida no router: ``a == b`` -> 422 (lutadores devem ser distintos); ``a`` ou
    ``b`` inexistente -> 404. Ambos existentes sem confronto direto retorna 200
    com lista vazia -- distinto do 404. A leitura sai do selector (router fino).
    """
    if a == b:
        raise HTTPException(status_code=422, detail="a e b devem ser lutadores distintos")
    if get_fighter_by_id(session, a) is None or get_fighter_by_id(session, b) is None:
        raise HTTPException(status_code=404, detail="Lutador não encontrado")
    detalhes = get_head_to_head(session, a, b)
    return HeadToHeadOut(
        fighter_a_id=a,
        fighter_b_id=b,
        bouts=[_to_bout_detail_out(detail) for detail in detalhes],
    )
