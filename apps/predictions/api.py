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

As duas respostas carregam ``features_freshness`` (SPEC 011, RF-05): o palpite vem do cache
as-of ``fighter_asof_features``, e quem lê o cache reporta se ele está defasado -- uma
comparação de contagem, não um recomputo de pipeline. Os caminhos de erro (404/422/503) seguem
sem o campo: eles não chegaram a ler o cache.

O endpoint de card é o primeiro da API v1 que **escreve** (registra cada predição em
``bout_predictions``): a gravação e o commit ficam no service, o router segue fino.

Desde o M9 (SPEC 011, Slice 05) os **dois** endpoints tratam a ausência de histórico como
estado, não como erro da requisição: 200 com ``unavailable_reason`` (prosa) e
``unavailable_code`` (enum, D-08). O matchup respondia 422 e passou a 200 pela D-04 -- a
página de confronto é alcançada por um clique vindo do card de um evento futuro, e o 422
forçaria a SPA a parsear a mensagem de erro. O matchup também passou a servir a
explicabilidade (``blocks`` por ablação local, ``attributes`` as-of crus), montada no service
``explain_matchup``; o router continua só validando, traduzindo o 503 e montando o schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from analysis.model import ARTIFACTS_DIR
from apps.bouts.models import BoutFighter
from apps.events.selectors import get_event_by_id
from apps.features.schemas import FeaturesFreshnessOut
from apps.features.selectors import get_features_freshness
from apps.fighters.selectors import get_fighter_by_id
from apps.predictions.schemas import (
    CardBoutPredictionOut,
    EventPredictionOut,
    MatchupAttributeOut,
    MatchupBlockInfluenceOut,
    MatchupFighterOut,
    MatchupPredictionOut,
)
from apps.predictions.services import (
    BoutCardPrediction,
    explain_matchup,
    predict_event_card,
)
from mma_analytics.db import get_session

router = APIRouter(prefix="/predict", tags=["predictions"])

# Contratos de erro do endpoint, declarados para entrarem no OpenAPI (e, por tabela, nos
# tipos gerados no web). O 422 NÃO é declarado aqui de propósito: o FastAPI já o documenta
# com o schema ``HTTPValidationError`` (validação de query + o único caso de negócio que
# resta, lutadores não-distintos); declará-lo aqui sobrescreveria esse schema. O 422 de
# "lutador sem histórico" **saiu** no M9: virou 200 com ``unavailable_code`` (D-04).
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


@router.get("/matchup", response_model=MatchupPredictionOut, responses=_MATCHUP_ERROR_RESPONSES)
def predict_matchup_endpoint(
    session: Annotated[Session, Depends(get_session)],
    artifacts_dir: Annotated[Path, Depends(get_artifacts_dir)],
    fighter_a: Annotated[int, Query(description="Id do primeiro lutador")],
    fighter_b: Annotated[int, Query(description="Id do segundo lutador")],
) -> MatchupPredictionOut:
    """Palpite neutro de canto para o confronto hipotético entre dois lutadores.

    Precedência dos erros, nesta ordem (DP-05f): ``fighter_a == fighter_b`` -> **422**
    (checado antes da existência); lutador inexistente -> **404**; artefato de modelo ausente
    -> **503**; lutador sem histórico no granular -> **200 indisponível**.

    O 503 **precede** o 200-indisponível porque o artefato é carregado antes de o cache ser
    consultado: servidor sem modelo é falha de servidor, não estado do par.

    Estreante **não** é mais 422 (D-04): responde 200 com probabilidades nulas,
    ``unavailable_reason`` em prosa e ``unavailable_code`` legível por máquina. A página de
    confronto é alcançada por um clique vindo do card de um evento futuro -- o usuário não
    formulou requisição errada, clicou numa luta que vai acontecer; e o 422 forçaria a SPA a
    parsear a mensagem de erro para distinguir "estreante" de falha real.

    As probabilidades são neutralizadas de canto (média das duas ordens), então a ordem dos
    parâmetros não muda o vencedor previsto.
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
        explicacao = explain_matchup(session, fighter_a, fighter_b, artifacts_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=_MODEL_UNAVAILABLE_DETAIL) from exc

    prob_a_wins = explicacao.prob_a_wins
    return MatchupPredictionOut(
        fighter_a=MatchupFighterOut(id=a.id, name=a.name),
        fighter_b=MatchupFighterOut(id=b.id, name=b.name),
        prob_a_wins=prob_a_wins,
        prob_b_wins=None if prob_a_wins is None else 1.0 - prob_a_wins,
        predicted_winner_id=explicacao.predicted_winner_id,
        unavailable_reason=explicacao.unavailable_reason,
        unavailable_code=explicacao.unavailable_code,
        blocks=[
            MatchupBlockInfluenceOut.model_validate(bloco, from_attributes=True)
            for bloco in explicacao.blocks
        ],
        attributes=[
            MatchupAttributeOut.model_validate(atributo, from_attributes=True)
            for atributo in explicacao.attributes
        ],
        features_freshness=FeaturesFreshnessOut.model_validate(get_features_freshness(session)),
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
        unavailable_code=previsao.unavailable_code,
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
        features_freshness=FeaturesFreshnessOut.model_validate(get_features_freshness(session)),
    )
