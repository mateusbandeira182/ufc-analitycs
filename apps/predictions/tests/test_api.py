"""Testes de API do endpoint de predição de confronto ``GET /api/v1/predict/matchup``.

Cobrem o router fino de predições: valida a entrada, resolve os dois lutadores do banco,
chama o serving (``analysis.predict.predict_matchup``) nas duas ordens de canto e devolve o
palpite **neutro de canto**. As asserções-chave são a neutralidade (trocar ``fighter_a`` e
``fighter_b`` produz o mesmo resultado, apenas com os lados espelhados) e o contrato de erro
(404 lutador inexistente, 422 mesmo lutador, 503 artefato de modelo ausente).

Estratégia de teste (Postgres transacional, sem tocar quota externa): reusa o histórico
sintético e o treino do teste de serving (``tests.analysis.test_predict``), persistindo um
modelo pequeno num ``tmp_path``. O diretório do artefato é injetado no endpoint sobrepondo a
dependência ``get_artifacts_dir``, e a ``Session`` é a sessão transacional da fixture.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.predictions.api import get_artifacts_dir
from mma_analytics.app import create_app
from mma_analytics.db import get_session
from tests.analysis.test_predict import _seed_history, _train_and_persist


def _client(db_session: Session, artifacts_dir: Path) -> Iterator[TestClient]:
    """``TestClient`` com ``get_session`` e ``get_artifacts_dir`` sobrepostos.

    A sessão aponta para a transação do teste; o diretório do artefato aponta para o
    ``tmp_path`` onde o modelo pequeno foi persistido (ou um diretório vazio, no caso 503).
    """
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_artifacts_dir] = lambda: artifacts_dir
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_matchup_devolve_probabilidades_complementares(db_session: Session, tmp_path: Path) -> None:
    """Confronto válido responde 200 com probabilidades somando ~1.0 e vencedor coerente."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    client = next(_client(db_session, tmp_path))
    resp = client.get(
        "/api/v1/predict/matchup",
        params={"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["fighter_a"] == {"id": ids["Fighter Alpha"], "name": "Fighter Alpha"}
    assert body["fighter_b"] == {"id": ids["Fighter Delta"], "name": "Fighter Delta"}
    assert 0.0 <= body["prob_a_wins"] <= 1.0
    assert 0.0 <= body["prob_b_wins"] <= 1.0
    assert body["prob_a_wins"] + body["prob_b_wins"] == pytest.approx(1.0)
    esperado = (
        ids["Fighter Alpha"] if body["prob_a_wins"] >= body["prob_b_wins"] else ids["Fighter Delta"]
    )
    assert body["predicted_winner_id"] == esperado


def test_matchup_e_neutro_de_canto(db_session: Session, tmp_path: Path) -> None:
    """Trocar a ordem dos parâmetros dá o MESMO resultado (palpite neutro de canto).

    Asserção-chave: a probabilidade de cada lutador vencer independe de ele ter sido passado
    como ``fighter_a`` ou ``fighter_b``. O modelo cru é sensível ao canto; a média das duas
    ordens neutraliza essa vantagem, então (A, B) e (B, A) elegem o mesmo vencedor previsto.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    a, b = ids["Fighter Alpha"], ids["Fighter Delta"]

    client = next(_client(db_session, tmp_path))
    ab = client.get("/api/v1/predict/matchup", params={"fighter_a": a, "fighter_b": b}).json()
    ba = client.get("/api/v1/predict/matchup", params={"fighter_a": b, "fighter_b": a}).json()

    # A probabilidade de A vencer é a mesma, esteja A no lado a (ab) ou no lado b (ba).
    assert ab["prob_a_wins"] == pytest.approx(ba["prob_b_wins"])
    assert ab["prob_b_wins"] == pytest.approx(ba["prob_a_wins"])
    # O vencedor previsto é idêntico -- a ordem dos parâmetros não muda o palpite.
    assert ab["predicted_winner_id"] == ba["predicted_winner_id"]


def test_matchup_lutador_inexistente_responde_404(db_session: Session, tmp_path: Path) -> None:
    """Um dos ids não corresponde a nenhum lutador -> 404."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    inexistente = max(ids.values()) + 10_000

    client = next(_client(db_session, tmp_path))
    resp = client.get(
        "/api/v1/predict/matchup",
        params={"fighter_a": ids["Fighter Alpha"], "fighter_b": inexistente},
    )

    assert resp.status_code == 404


def test_matchup_mesmo_lutador_responde_422(db_session: Session, tmp_path: Path) -> None:
    """Mesmo lutador dos dois lados -> 422 (o confronto exige lutadores distintos)."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    alpha = ids["Fighter Alpha"]

    client = next(_client(db_session, tmp_path))
    resp = client.get("/api/v1/predict/matchup", params={"fighter_a": alpha, "fighter_b": alpha})

    assert resp.status_code == 422


def test_matchup_mesmo_lutador_tem_precedencia_sobre_inexistencia(
    db_session: Session, tmp_path: Path
) -> None:
    """Dois ids iguais e inexistentes ainda respondem 422 (validado antes da existência)."""
    _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    client = next(_client(db_session, tmp_path))
    resp = client.get(
        "/api/v1/predict/matchup", params={"fighter_a": 999_999, "fighter_b": 999_999}
    )

    assert resp.status_code == 422


def test_matchup_artefato_ausente_responde_503(db_session: Session, tmp_path: Path) -> None:
    """Sem artefato de modelo treinado -> 503 (mensagem clara), nunca 500 cru."""
    ids = _seed_history(db_session)
    # Não treina: tmp_path fica sem o artefato joblib.

    client = next(_client(db_session, tmp_path))
    resp = client.get(
        "/api/v1/predict/matchup",
        params={"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]},
    )

    assert resp.status_code == 503
    assert "modelo" in resp.json()["detail"].lower()
