"""Router fino de events (somente leitura).

Valida a entrada, delega a leitura ao selector e devolve o response schema; não
executa query no router. ``list`` responde o envelope ``Page[EventOut]`` (recentes
primeiro) e ``detail`` levanta 404 quando o event não existe, compondo o card de
bouts a partir do selector.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from apps.bouts.models import Bout
from apps.events.schemas import BoutCardFighterOut, BoutCardOut, EventDetailOut, EventOut
from apps.events.selectors import get_event_by_id, list_event_bouts, list_events
from mma_analytics.db import get_session
from mma_analytics.pagination import Page, PageParams, page_params

router = APIRouter(prefix="/events", tags=["events"])


def _to_bout_card_out(bout: Bout) -> BoutCardOut:
    """Monta o card de uma luta com a dupla de participantes (id, nome e canto).

    Os participantes vêm de ``bout.bout_fighters`` (eager-loaded no selector), com
    o nome via ``BoutFighter.fighter`` -- por isso a construção é explícita.
    """
    return BoutCardOut(
        id=bout.id,
        winner_id=bout.winner_id,
        method=bout.method,
        round=bout.round,
        ending_time_seconds=bout.ending_time_seconds,
        weight_class=bout.weight_class,
        source=bout.source,
        fighters=[
            BoutCardFighterOut(fighter_id=bf.fighter_id, name=bf.fighter.name, corner=bf.corner)
            for bf in bout.bout_fighters
        ],
    )


@router.get("", response_model=Page[EventOut])
def list_events_endpoint(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[EventOut]:
    """Lista events paginados, mais recentes primeiro."""
    rows, total = list_events(session, limit=params.limit, offset=params.offset)
    return Page[EventOut](
        items=[EventOut.model_validate(row) for row in rows],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )


@router.get("/{event_id}", response_model=EventDetailOut)
def get_event_endpoint(
    event_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> EventDetailOut:
    """Detalha um event pelo id com o card de bouts; 404 quando não existe."""
    event = get_event_by_id(session, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event não encontrado")
    bouts = list_event_bouts(session, event_id)
    return EventDetailOut(
        **EventOut.model_validate(event).model_dump(),
        bouts=[_to_bout_card_out(bout) for bout in bouts],
    )
