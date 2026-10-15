"""Router fino de events (somente leitura).

Valida a entrada, delega a leitura ao selector e devolve o response schema; não
executa query no router. ``list`` responde o envelope ``Page[EventOut]`` (recentes
primeiro, opcionalmente particionado por ``when``) e ``detail`` levanta 404 quando o
event não existe, compondo o card de bouts, a contagem por método e os destaques a
partir dos selectors.

**A ordem de declaração das rotas é load-bearing**: ``/latest`` precede
``/{event_id}``, senão o FastAPI casa ``event_id="latest"`` e devolve 422 -- um erro
silenciosamente plausível ("id inválido") numa rota que deveria responder 200.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.bouts.models import Bout
from apps.events.models import Event
from apps.events.schemas import (
    BoutCardFighterOut,
    BoutCardOut,
    BoutHighlightOut,
    EventDetailOut,
    EventHighlightsOut,
    EventOut,
    EventSummaryOut,
    FinishHighlightOut,
    UpsetHighlightOut,
)
from apps.events.selectors import (
    EventHighlights,
    EventWhen,
    collect_event_highlights,
    get_event_by_id,
    get_latest_completed_event,
    list_event_bouts,
    list_events,
    summarize_event_bouts,
)
from apps.fighters.models import Fighter
from apps.predictions.schemas import MatchupFighterOut
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
        status=bout.status,
        card_section=bout.card_section,
        bout_order=bout.bout_order,
        fighters=[
            BoutCardFighterOut(
                fighter_id=bf.fighter_id,
                name=bf.fighter.name,
                corner=bf.corner,
                headshot_url=bf.fighter.headshot_url,
            )
            for bf in bout.bout_fighters
        ],
    )


def _to_fighter_out(fighter: Fighter) -> MatchupFighterOut:
    """Resolve o lutador de um destaque em id + nome.

    ``MatchupFighterOut`` é **reusado** de ``apps.predictions.schemas``: id + nome é
    exatamente o que o destaque precisa, e um segundo schema idêntico seria duplicação no
    contrato e em ``web/src/api/types.ts``.
    """
    return MatchupFighterOut(id=fighter.id, name=fighter.name)


def _to_highlights_out(destaques: EventHighlights) -> EventHighlightsOut:
    """Converte os destaques calculados do selector no schema servido.

    Cada destaque permanece nulo quando não há base (RF-13) -- nunca é substituído por
    zero. Um ``value: 0`` que chega preenchido é medição, e passa adiante como tal.
    """
    finalizacao = destaques.fastest_finish
    sig_strikes = destaques.most_sig_strikes
    quedas = destaques.most_takedowns
    zebra = destaques.biggest_upset
    return EventHighlightsOut(
        fastest_finish=(
            None
            if finalizacao is None
            else FinishHighlightOut(
                bout_id=finalizacao.bout_id,
                fighter=_to_fighter_out(finalizacao.fighter),
                method=finalizacao.method,
                round=finalizacao.round,
                ending_time_seconds=finalizacao.ending_time_seconds,
            )
        ),
        most_sig_strikes=(
            None
            if sig_strikes is None
            else BoutHighlightOut(
                bout_id=sig_strikes.bout_id,
                fighter=_to_fighter_out(sig_strikes.fighter),
                value=sig_strikes.value,
            )
        ),
        most_takedowns=(
            None
            if quedas is None
            else BoutHighlightOut(
                bout_id=quedas.bout_id,
                fighter=_to_fighter_out(quedas.fighter),
                value=quedas.value,
            )
        ),
        biggest_upset=(
            None
            if zebra is None
            else UpsetHighlightOut(
                bout_id=zebra.bout_id,
                winner=_to_fighter_out(zebra.winner),
                prob_of_winner=zebra.prob_of_winner,
                n_bouts_with_prediction=zebra.n_bouts_with_prediction,
                n_bouts=zebra.n_bouts,
            )
        ),
    )


def _to_event_detail_out(session: Session, event: Event) -> EventDetailOut:
    """Compõe o detalhe de um event: card, contagem por método e destaques.

    Dois callsites concretos -- ``/events/latest`` e ``/events/{event_id}`` servem a mesma
    forma (DEC-4), e um ``EventLatestOut`` sem campo novo seria elemento sem conteúdo,
    duplicando o schema no OpenAPI e em ``web/src/api/types.ts`` sem benefício para nenhum
    leitor.

    ``highlights`` só é calculado quando há resumo: os dois declaram ausência pelo **mesmo**
    critério (nenhuma luta realizada classificável), e ``n_bouts`` vem do resumo para que a
    zebra e a contagem por método falem do mesmo denominador.
    """
    bouts = list_event_bouts(session, event.id)
    resumo = summarize_event_bouts(bouts)
    destaques = (
        None
        if resumo is None
        else collect_event_highlights(session, bouts=bouts, n_bouts=resumo.n_bouts)
    )
    return EventDetailOut(
        **EventOut.model_validate(event).model_dump(),
        bouts=[_to_bout_card_out(bout) for bout in bouts],
        summary=None if resumo is None else EventSummaryOut.model_validate(resumo),
        highlights=None if destaques is None else _to_highlights_out(destaques),
    )


@router.get("", response_model=Page[EventOut])
def list_events_endpoint(
    session: Annotated[Session, Depends(get_session)],
    params: Annotated[PageParams, Depends(page_params)],
    when: Annotated[EventWhen | None, Query()] = None,
) -> Page[EventOut]:
    """Lista events paginados; sem ``when``, mais recentes primeiro.

    ``when=past|upcoming`` particiona o acervo (a partição deriva do ``status`` da luta,
    nunca da data sozinha) e o ``total`` passa a refletir o conjunto filtrado. O tipo é
    ``Literal``, não ``str``: sem ele o FastAPI **ignoraria** um valor inválido como
    ``when=futuro`` e devolveria o acervo inteiro com 200 -- um defeito que se parece com
    sucesso. Com o ``Literal``, a resposta é 422.
    """
    rows, total = list_events(session, when=when, limit=params.limit, offset=params.offset)
    return Page[EventOut](
        items=[EventOut.model_validate(row) for row in rows],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )


@router.get("/latest", response_model=EventDetailOut)
def get_latest_event_endpoint(
    session: Annotated[Session, Depends(get_session)],
) -> EventDetailOut:
    """Detalha o último event **realizado**; 404 quando o acervo não tem nenhum.

    Declarada **antes** de ``/{event_id}`` de propósito: na ordem inversa o FastAPI casaria
    ``event_id="latest"`` e devolveria 422.

    404 (e não 200 com corpo nulo) porque um corpo nulo tornaria o tipo anulável em todo
    consumidor do contrato por causa de um estado de acervo vazio.
    """
    event = get_latest_completed_event(session)
    if event is None:
        raise HTTPException(status_code=404, detail="Nenhum event realizado no acervo")
    return _to_event_detail_out(session, event)


@router.get("/{event_id}", response_model=EventDetailOut)
def get_event_endpoint(
    event_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> EventDetailOut:
    """Detalha um event pelo id com o card, a contagem por método e os destaques; 404 se não."""
    event = get_event_by_id(session, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event não encontrado")
    return _to_event_detail_out(session, event)
