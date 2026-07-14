"""Router fino de predições (somente leitura): serving do modelo na API v1.

Expõe ``GET /api/v1/predict/matchup``: valida a entrada, resolve os dois lutadores do banco e
delega o cálculo ao serving já pronto (``analysis.predict.predict_matchup``), devolvendo o
palpite **neutro de canto**. O modelo cru é sensível ao canto (aprendeu a vantagem do
vermelho), logo a predição de uma única ordem não é simétrica; o router chama o serving nas
duas ordens (A vs B e B vs A) e usa a média como probabilidade neutra de A vencer -- assim a
ordem dos parâmetros não altera o resultado.

Validação no próprio router (padrão do head-to-head, ADR 0003): ``fighter_a == fighter_b`` ->
422, checado ANTES da existência (dois ids iguais e inexistentes ainda respondem 422); lutador
inexistente -> 404 (via ``get_fighter_by_id``); artefato de modelo ausente -> 503 (mensagem
clara, nunca 500 cru). O diretório do artefato é uma dependência (``get_artifacts_dir``) para
ser sobreposta nos testes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from analysis.model import ARTIFACTS_DIR
from analysis.predict import predict_matchup
from apps.fighters.selectors import get_fighter_by_id
from apps.predictions.schemas import MatchupFighterOut, MatchupPredictionOut
from mma_analytics.db import get_session

router = APIRouter(prefix="/predict", tags=["predictions"])


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


@router.get("/matchup", response_model=MatchupPredictionOut)
def predict_matchup_endpoint(
    session: Annotated[Session, Depends(get_session)],
    artifacts_dir: Annotated[Path, Depends(get_artifacts_dir)],
    fighter_a: Annotated[int, Query(description="Id do primeiro lutador")],
    fighter_b: Annotated[int, Query(description="Id do segundo lutador")],
) -> MatchupPredictionOut:
    """Palpite neutro de canto para o confronto hipotético entre dois lutadores.

    ``fighter_a == fighter_b`` -> 422; lutador inexistente -> 404; artefato de modelo ausente
    -> 503. As probabilidades são neutralizadas de canto (média das duas ordens), então a
    ordem dos parâmetros não muda o vencedor previsto.
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
        raise HTTPException(
            status_code=503,
            detail="Modelo preditivo indisponível: artefato treinado não encontrado.",
        ) from exc

    prob_b_wins = 1.0 - prob_a_wins
    # O vencedor previsto é função pura do par (não da ordem dos parâmetros): maior
    # probabilidade vence e, no empate exato, desempata pelo menor id -- assim (A, B) e
    # (B, A) elegem sempre o mesmo lutador, coerente com a neutralidade das probabilidades.
    if prob_a_wins > prob_b_wins:
        predicted_winner_id = fighter_a
    elif prob_b_wins > prob_a_wins:
        predicted_winner_id = fighter_b
    else:
        predicted_winner_id = min(fighter_a, fighter_b)
    return MatchupPredictionOut(
        fighter_a=MatchupFighterOut(id=a.id, name=a.name),
        fighter_b=MatchupFighterOut(id=b.id, name=b.name),
        prob_a_wins=prob_a_wins,
        prob_b_wins=prob_b_wins,
        predicted_winner_id=predicted_winner_id,
    )
