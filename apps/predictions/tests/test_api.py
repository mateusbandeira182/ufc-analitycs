"""Testes de API dos endpoints de predição (``/predict/matchup`` e ``/predict/event/{id}``).

Cobrem o router fino de predições: valida a entrada, resolve os lutadores do banco, chama o
serving nas duas ordens de canto e devolve o palpite **neutro de canto**. No matchup, as
asserções-chave são a neutralidade (trocar ``fighter_a`` e ``fighter_b`` dá o mesmo resultado,
com os lados espelhados) e o contrato de erro (404 lutador inexistente, 422 mesmo lutador ou
sem histórico, 503 artefato ausente). "Sem histórico" tem dois cenários distintos e ambos
respondem 422: lutador sem nenhuma aparição no granular e lutador cuja **única** luta cadastrada
é futura -- o segundo é o que discrimina o recorte por data da Slice 08.

No card de evento, o que se afirma é a consistência com o matchup para o mesmo par (o batching
não muda o palpite) e o comportamento **por luta**: uma luta impredizível volta com
``unavailable_reason`` e status 200, sem derrubar as demais -- diferente do matchup, onde a
mesma situação é 422 porque é erro da requisição. Contrato de erro do card: 404 evento
inexistente, 503 artefato ausente, 200 com ``bouts`` vazio para evento sem lutas.

Estratégia de teste (Postgres transacional, sem tocar quota externa): reusa o histórico
sintético e o treino do teste de serving (``tests.analysis.test_predict``), persistindo um
modelo pequeno num ``tmp_path``. O diretório do artefato é injetado no endpoint sobrepondo a
dependência ``get_artifacts_dir``, e a ``Session`` é a sessão transacional da fixture.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from analysis.model import load_artifact
from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.events.selectors import list_event_bouts
from apps.predictions.api import get_artifacts_dir
from mma_analytics.app import create_app
from mma_analytics.db import get_session
from tests.analysis.test_predict import (
    _make_fighter,
    _seed_future_bout,
    _seed_history,
    _train_and_persist,
)


@contextmanager
def _client(db_session: Session, artifacts_dir: Path) -> Iterator[TestClient]:
    """``TestClient`` com ``get_session`` e ``get_artifacts_dir`` sobrepostos.

    A sessão aponta para a transação do teste; o diretório do artefato aponta para o
    ``tmp_path`` onde o modelo pequeno foi persistido (ou um diretório vazio, no caso 503).
    Context manager: o ``with`` garante que o ``TestClient`` fecha e que os
    ``dependency_overrides`` são limpos ao fim de cada teste (sem vazamento entre casos).
    """
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_artifacts_dir] = lambda: artifacts_dir
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_matchup_devolve_probabilidades_complementares(db_session: Session, tmp_path: Path) -> None:
    """Confronto válido responde 200 com probabilidades somando ~1.0 e vencedor coerente."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with _client(db_session, tmp_path) as client:
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

    with _client(db_session, tmp_path) as client:
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

    with _client(db_session, tmp_path) as client:
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

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup", params={"fighter_a": alpha, "fighter_b": alpha}
        )

    assert resp.status_code == 422


def test_matchup_mesmo_lutador_tem_precedencia_sobre_inexistencia(
    db_session: Session, tmp_path: Path
) -> None:
    """Dois ids iguais e inexistentes ainda respondem 422 (validado antes da existência)."""
    _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup", params={"fighter_a": 999_999, "fighter_b": 999_999}
        )

    assert resp.status_code == 422


def test_matchup_artefato_ausente_responde_503(db_session: Session, tmp_path: Path) -> None:
    """Sem artefato de modelo treinado -> 503 (mensagem clara), nunca 500 cru."""
    ids = _seed_history(db_session)
    # Não treina: tmp_path fica sem o artefato joblib.

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]},
        )

    assert resp.status_code == 503
    assert "modelo" in resp.json()["detail"].lower()


def test_matchup_lutador_sem_historico_responde_422(db_session: Session, tmp_path: Path) -> None:
    """Lutador existe no banco mas não tem lutas no granular -> 422 (não 500 cru).

    O serving levanta ``ValueError`` (sem features as-of); o router traduz para 422 para a SPA
    tratar "sem histórico" como estado esperado, distinto do 404 (lutador inexistente).
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    sem_historico = _make_fighter("Fighter Sem Historico", 195)
    db_session.add(sem_historico)
    db_session.flush()

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": int(sem_historico.id)},
        )

    assert resp.status_code == 422
    assert "histórico" in resp.json()["detail"].lower()


def test_matchup_lutador_so_com_luta_futura_responde_422(
    db_session: Session, tmp_path: Path
) -> None:
    """Lutador cuja ÚNICA luta cadastrada é **futura** -> 422, não um palpite fabricado.

    Regressão da correção da Slice 08: a luta do card já está no granular, mas ainda não
    aconteceu -- não há box-score nenhum a extrair dela. Sem o recorte por data na checagem de
    elegibilidade, o estreante passaria no teste de histórico pela própria luta que se quer
    prever e o endpoint responderia 200 com 0.5 fabricado sobre um vetor inteiramente ``NaN``.
    Distinto de ``test_matchup_lutador_sem_historico_responde_422``, onde o lutador não tem
    nenhuma aparição no granular.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    estreante = _make_fighter("Fighter Estreante", 195)
    db_session.add(estreante)
    db_session.flush()
    _seed_future_bout(db_session, ids["Fighter Alpha"], int(estreante.id))

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": int(estreante.id)},
        )

    assert resp.status_code == 422
    assert "histórico" in resp.json()["detail"].lower()


def _seed_card(db_session: Session, ids: dict[str, int], pares: list[tuple[str, str]]) -> int:
    """Semeia um evento cujo card tem uma luta por par (nomes do roster) e devolve o event_id.

    As lutas ainda não têm vencedor nem box-score (``winner_id`` e as estatísticas nulas): é um
    card **futuro**, que é o que o endpoint de predição de evento existe para servir. O
    ``method`` é preenchido porque a coluna é NOT NULL no schema atual (que nasceu para
    eventos já ocorridos) -- é placeholder, nada no endpoint o consome.
    """
    event = Event(name="UFC Card Futuro", date=date(2026, 12, 31), location=None, source="kaggle")
    db_session.add(event)
    db_session.flush()
    for nome_red, nome_blue in pares:
        bout = Bout(
            event_id=event.id,
            winner_id=None,
            method=BoutMethod.DECISION,
            round=None,
            ending_time_seconds=None,
            weight_class=None,
            source="kaggle",
        )
        db_session.add(bout)
        db_session.flush()
        db_session.add_all(
            [
                BoutFighter(
                    bout_id=bout.id,
                    fighter_id=ids[nome_red],
                    corner=Corner.RED,
                    source="kaggle",
                ),
                BoutFighter(
                    bout_id=bout.id,
                    fighter_id=ids[nome_blue],
                    corner=Corner.BLUE,
                    source="kaggle",
                ),
            ]
        )
        db_session.flush()
    return int(event.id)


def test_event_preve_todas_as_lutas_do_card(db_session: Session, tmp_path: Path) -> None:
    """Card completo responde 200 com uma entrada por luta, na ordem determinística do card."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(
        db_session, ids, [("Fighter Alpha", "Fighter Delta"), ("Fighter Bravo", "Fighter Charlie")]
    )
    esperado = list_event_bouts(db_session, event_id)

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 200
    body = resp.json()
    artefato = load_artifact(tmp_path)
    assert body["event_id"] == event_id
    assert body["model_version"] == artefato.trained_at
    assert body["n_features"] == len(artefato.feature_names)
    assert [item["bout_id"] for item in body["bouts"]] == [bout.id for bout in esperado]
    for item in body["bouts"]:
        assert item["unavailable_reason"] is None
        assert 0.0 <= item["prob_red_wins"] <= 1.0
        assert item["prob_red_wins"] + item["prob_blue_wins"] == pytest.approx(1.0)
        assert item["predicted_winner_id"] in {
            item["fighter_red"]["id"],
            item["fighter_blue"]["id"],
        }


def test_event_bate_com_o_matchup_do_mesmo_par(db_session: Session, tmp_path: Path) -> None:
    """A predição do card é a MESMA do endpoint de matchup para o par -- neutra de canto.

    Prova que o batching (uma passada por ordem de canto para o card inteiro) não muda o
    resultado e que os dois caminhos elegem o mesmo vencedor previsto.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(
        db_session, ids, [("Fighter Alpha", "Fighter Delta"), ("Fighter Bravo", "Fighter Charlie")]
    )

    with _client(db_session, tmp_path) as client:
        card = client.get(f"/api/v1/predict/event/{event_id}").json()
        matchup = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]},
        ).json()

    luta = next(item for item in card["bouts"] if item["fighter_red"]["id"] == ids["Fighter Alpha"])
    assert luta["prob_red_wins"] == pytest.approx(matchup["prob_a_wins"])
    assert luta["prob_blue_wins"] == pytest.approx(matchup["prob_b_wins"])
    assert luta["predicted_winner_id"] == matchup["predicted_winner_id"]


def test_openapi_inclui_path_de_predicao_de_evento(client: TestClient) -> None:
    """O contrato do card entra no OpenAPI (e, por tabela, nos tipos gerados no web)."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/predict/event/{event_id}" in paths


def test_event_inexistente_responde_404(db_session: Session, tmp_path: Path) -> None:
    """Id que não corresponde a nenhum evento -> 404, antes de qualquer predição."""
    _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with _client(db_session, tmp_path) as client:
        resp = client.get("/api/v1/predict/event/999999")

    assert resp.status_code == 404


def test_event_artefato_ausente_responde_503(db_session: Session, tmp_path: Path) -> None:
    """Sem artefato de modelo treinado -> 503 (mensagem clara), nunca 500 cru."""
    ids = _seed_history(db_session)
    event_id = _seed_card(db_session, ids, [("Fighter Alpha", "Fighter Delta")])
    # Não treina: tmp_path fica sem o artefato joblib.

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 503
    assert "modelo" in resp.json()["detail"].lower()


def test_event_sem_lutas_responde_card_vazio(db_session: Session, tmp_path: Path) -> None:
    """Evento existente sem lutas cadastradas -> 200 com ``bouts`` vazio (não 404)."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(db_session, ids, [])

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["bouts"] == []
    assert body["model_version"] == load_artifact(tmp_path).trained_at


def test_event_luta_sem_historico_nao_derruba_o_card(db_session: Session, tmp_path: Path) -> None:
    """Uma luta impredizível volta com motivo; as demais do card seguem preditas (200).

    Diferença deliberada em relação ao matchup isolado: lá "sem histórico" é 422 (erro da
    requisição), aqui é estado de **um item** do card.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    sem_historico = _make_fighter("Fighter Sem Historico", 195)
    db_session.add(sem_historico)
    db_session.flush()
    ids["Fighter Sem Historico"] = int(sem_historico.id)
    event_id = _seed_card(
        db_session,
        ids,
        [("Fighter Alpha", "Fighter Sem Historico"), ("Fighter Bravo", "Fighter Charlie")],
    )

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 200
    impredizivel, predita = resp.json()["bouts"]
    assert impredizivel["prob_red_wins"] is None
    assert impredizivel["prob_blue_wins"] is None
    assert impredizivel["predicted_winner_id"] is None
    assert str(ids["Fighter Sem Historico"]) in impredizivel["unavailable_reason"]
    # Os cantos continuam expostos: o consumidor vê a luta no card, só sem palpite.
    assert impredizivel["fighter_red"]["id"] == ids["Fighter Alpha"]
    # A outra luta do MESMO card foi predita normalmente.
    assert predita["unavailable_reason"] is None
    assert predita["prob_red_wins"] is not None
