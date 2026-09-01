"""Router fino de predições: serving do modelo na API v1.

Expõe ``GET /api/v1/predict/matchup`` (um confronto hipotético) e
``GET /api/v1/predict/event/{event_id}`` (o card completo de um evento): valida a entrada,
resolve os lutadores do banco e delega o cálculo ao serving já pronto (``analysis.predict``),
devolvendo o palpite **neutro de canto**. O modelo cru é sensível ao canto (aprendeu a vantagem
do vermelho), logo a predição de uma única ordem não é simétrica; o serving é chamado nas duas
ordens (A vs B e B vs A) e a média é a probabilidade neutra de A vencer -- assim a ordem dos
parâmetros não altera o resultado.

Validação no próprio router (padrão do head-to-head, ADR 0003): ``fighter_a == fighter_b`` ->
422, checado ANTES da existência (dois ids iguais e inexistentes ainda respondem 422); lutador
inexistente -> 404 (via ``get_fighter_by_id``); artefato de modelo ausente -> 503 (mensagem
clara, nunca 500 cru). O diretório do artefato é uma dependência (``get_artifacts_dir``) para
ser sobreposta nos testes.

O endpoint de card é o primeiro da API v1 que **escreve** (registra cada predição em
``bout_predictions``): a gravação e o commit ficam no service, o router segue fino. Nele, a
ausência de histórico é estado de uma luta (``unavailable_reason``, status 200) e não 422 --
no card, uma luta impredizível não é erro da requisição.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from analysis.model import ARTIFACTS_DIR
from analysis.predict import predict_matchup
from apps.bouts.models import BoutFighter
from apps.events.selectors import get_event_by_id
from apps.fighters.selectors import get_fighter_by_id
from apps.predictions.schemas import (
    CardBoutPredictionOut,
    EventPredictionOut,
    MatchupFighterOut,
    MatchupPredictionOut,
)
from apps.predictions.services import (
    BoutCardPrediction,
    predict_event_card,
    predicted_winner_id,
)
from mma_analytics.db import get_session

router = APIRouter(prefix="/predict", tags=["predictions"])

# Contratos de erro do endpoint, declarados para entrarem no OpenAPI (e, por tabela, nos
# tipos gerados no web). O 422 NÃO é declarado aqui de propósito: o FastAPI já o documenta
# com o schema ``HTTPValidationError`` (validação de query + os casos de negócio do router --
# lutadores não-distintos e lutador sem histórico); declará-lo aqui sobrescreveria esse schema.
_MATCHUP_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {"description": "Lutador não encontrado"},
    503: {"description": "Modelo preditivo indisponível: artefato treinado ausente"},
}

# O card não declara 422 de negócio: lutador sem histórico ali é estado de UMA luta
# (``unavailable_reason``), não erro da requisição -- ver o docstring do endpoint.
_EVENT_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {"description": "Event não encontrado"},
    503: {"description": "Modelo preditivo indisponível: artefato treinado ausente"},
}

_MODEL_UNAVAILABLE_DETAIL = "Modelo preditivo indisponível: artefato treinado não encontrado."


def get_artifacts_dir() -> Path:
    """Dependência FastAPI: diretório do artefato do modelo (sobreposto nos testes)."""
    return ARTIFACTS_DIR


def _neutral_prob_a_wins(
    session: Session, fighter_a_id: int, fighter_b_id: int, directory: Path
) -> float:
    """Probabilidade neutra de A vencer: média das duas ordens de canto.

    ``predict_matchup(a, b).prob_a_wins`` avalia A no canto vermelho;
    ``predict_matchup(b, a).prob_b_wins`` avalia A no canto azul. A média cancela a vantagem
    de canto que o modelo aprendeu, tornando o palpite invariante à ordem dos parâmetros.
    """
    forward = predict_matchup(session, fighter_a_id, fighter_b_id, directory)
    reverse = predict_matchup(session, fighter_b_id, fighter_a_id, directory)
    return (forward.prob_a_wins + reverse.prob_b_wins) / 2.0


@router.get("/matchup", response_model=MatchupPredictionOut, responses=_MATCHUP_ERROR_RESPONSES)
def predict_matchup_endpoint(
    session: Annotated[Session, Depends(get_session)],
    artifacts_dir: Annotated[Path, Depends(get_artifacts_dir)],
    fighter_a: Annotated[int, Query(description="Id do primeiro lutador")],
    fighter_b: Annotated[int, Query(description="Id do segundo lutador")],
) -> MatchupPredictionOut:
    """Palpite neutro de canto para o confronto hipotético entre dois lutadores.

    ``fighter_a == fighter_b`` -> 422; lutador inexistente -> 404; lutador sem histórico no
    granular (existe, mas não tem lutas para derivar features as-of) -> 422; artefato de modelo
    ausente -> 503. As probabilidades são neutralizadas de canto (média das duas ordens), então
    a ordem dos parâmetros não muda o vencedor previsto.
    """
    if fighter_a == fighter_b:
        raise HTTPException(
            status_code=422, detail="fighter_a e fighter_b devem ser lutadores distintos"
        )
    a = get_fighter_by_id(session, fighter_a)
    b = get_fighter_by_id(session, fighter_b)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="Lutador não encontrado")

    try:
        prob_a_wins = _neutral_prob_a_wins(session, fighter_a, fighter_b, artifacts_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=_MODEL_UNAVAILABLE_DETAIL) from exc
    except ValueError as exc:
        # Lutador válido (existe no banco), mas sem lutas no granular: a pipeline de features
        # as-of não tem base para predizer. Traduzido para 422 (nunca 500 cru) para a SPA
        # tratar "sem histórico" como estado esperado.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return MatchupPredictionOut(
        fighter_a=MatchupFighterOut(id=a.id, name=a.name),
        fighter_b=MatchupFighterOut(id=b.id, name=b.name),
        prob_a_wins=prob_a_wins,
        prob_b_wins=1.0 - prob_a_wins,
        predicted_winner_id=predicted_winner_id(fighter_a, fighter_b, prob_a_wins),
    )


def _corner_out(bout_fighter: BoutFighter | None) -> MatchupFighterOut | None:
    """Identidade do canto (id + nome), ``None`` quando a luta não tem esse canto cadastrado."""
    if bout_fighter is None:
        return None
    return MatchupFighterOut(id=bout_fighter.fighter_id, name=bout_fighter.fighter.name)


def _to_card_bout_out(previsao: BoutCardPrediction) -> CardBoutPredictionOut:
    """Converte a predição de uma luta do card no schema de saída."""
    return CardBoutPredictionOut(
        bout_id=previsao.bout_id,
        fighter_red=_corner_out(previsao.red),
        fighter_blue=_corner_out(previsao.blue),
        prob_red_wins=previsao.prob_red_wins,
        prob_blue_wins=previsao.prob_blue_wins,
        predicted_winner_id=previsao.predicted_winner_id,
        unavailable_reason=previsao.unavailable_reason,
    )


@router.get(
    "/event/{event_id}", response_model=EventPredictionOut, responses=_EVENT_ERROR_RESPONSES
)
def predict_event_card_endpoint(
    event_id: int,
    session: Annotated[Session, Depends(get_session)],
    artifacts_dir: Annotated[Path, Depends(get_artifacts_dir)],
) -> EventPredictionOut:
    """Palpite neutro de canto para todas as lutas do card de um evento.

    Event inexistente -> 404; artefato de modelo ausente -> 503; evento sem lutas -> 200 com
    ``bouts`` vazio. Uma luta impredizível (lutador sem histórico no granular, card mal
    formado) **não** derruba o card: volta com probabilidades nulas e ``unavailable_reason``
    preenchido, status 200 -- diferença deliberada em relação ao matchup isolado, onde a mesma
    situação é 422 porque é erro da própria requisição.

    Cada luta predita é registrada em ``bout_predictions`` com ``source="api"``, idempotente
    por ``(bout_id, model_version)``. A idempotência é **observável**: repetir o request não
    envelhece o registro histórico, porque ``record_prediction`` preserva ``predicted_at`` e
    ``source`` da primeira gravação. É o que sustenta o verbo GET aqui apesar da escrita --
    render repetido da tela não muda nada que alguém possa ler depois.
    """
    if get_event_by_id(session, event_id) is None:
        raise HTTPException(status_code=404, detail="Event não encontrado")
    try:
        card = predict_event_card(session, event_id, artifacts_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=_MODEL_UNAVAILABLE_DETAIL) from exc
    return EventPredictionOut(
        event_id=card.event_id,
        model_version=card.model_version,
        n_features=card.n_features,
        bouts=[_to_card_bout_out(previsao) for previsao in card.bouts],
    )
