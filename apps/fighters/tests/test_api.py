"""Testes de API da Slice 01: esqueleto FastAPI + list/detail de fighters.

Exercitam o router fino sobre o Postgres de teste transacional (fixture ``client``,
com ``get_session`` sobreposto). Cobrem o contrato OpenAPI, o envelope de paginação,
o filtro por nome, o detalhe com 404 e a natureza somente-leitura da API v1.
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.fighters.tests.factories import FighterFactory
from mma_analytics.app import create_app

_MUTATING_VERBS = {"POST", "PUT", "PATCH", "DELETE"}


def _seed(db_session: Session, name: str) -> int:
    """Persiste um fighter com o ``name`` dado e devolve o id gerado."""
    fighter = FighterFactory.build(name=name)
    db_session.add(fighter)
    db_session.flush()
    return fighter.id


def _seed_history(db_session: Session) -> int:
    """Semeia o histórico de um lutador: dois eventos, com um oponente no mais antigo.

    Devolve o id do lutador consultado. O evento antigo tem os dois cantos (com
    stats distintas por canto, para provar que o histórico traz o canto consultado
    e não o oponente); o evento recente tem só o lutador consultado, vencedor.
    """
    fighter = FighterFactory.build(name="Max Holloway")
    opponent = FighterFactory.build(name="Dustin Poirier")
    db_session.add_all([fighter, opponent])
    db_session.flush()

    older_event = EventFactory.build(name="UFC 236", date=date(2019, 4, 13))
    newer_event = EventFactory.build(name="UFC 300", date=date(2024, 4, 13))
    db_session.add_all([older_event, newer_event])
    db_session.flush()

    older_bout = BoutFactory.build(
        event_id=older_event.id, winner_id=opponent.id, method=BoutMethod.DECISION
    )
    newer_bout = BoutFactory.build(
        event_id=newer_event.id, winner_id=fighter.id, method=BoutMethod.KO_TKO
    )
    db_session.add_all([older_bout, newer_bout])
    db_session.flush()

    db_session.add_all(
        [
            BoutFighterFactory.build(
                bout_id=older_bout.id,
                fighter_id=fighter.id,
                corner=Corner.RED,
                sig_strikes_landed=20,
            ),
            BoutFighterFactory.build(
                bout_id=older_bout.id,
                fighter_id=opponent.id,
                corner=Corner.BLUE,
                sig_strikes_landed=99,
            ),
            BoutFighterFactory.build(
                bout_id=newer_bout.id,
                fighter_id=fighter.id,
                corner=Corner.RED,
                sig_strikes_landed=50,
            ),
        ]
    )
    db_session.flush()
    return fighter.id


def test_docs_e_openapi_respondem(client: TestClient) -> None:
    """``/docs`` e ``/openapi.json`` respondem 200 e a versão do contrato é ``1``."""
    docs = client.get("/docs")
    assert docs.status_code == 200

    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    assert openapi.json()["info"]["version"] == "1"


def test_openapi_inclui_path_de_fighters(client: TestClient) -> None:
    """O contrato OpenAPI expõe as rotas de fighters sob ``/api/v1``."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/fighters" in paths
    assert "/api/v1/fighters/{fighter_id}" in paths
    assert "/api/v1/fighters/{fighter_id}/bouts" in paths


def test_list_fighters_retorna_envelope_page_com_source(
    client: TestClient, db_session: Session
) -> None:
    """A lista devolve o envelope ``Page`` e cada item traz ``source`` (RF-09)."""
    _seed(db_session, "Jon Jones")

    body = client.get("/api/v1/fighters").json()

    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["items"][0]["name"] == "Jon Jones"
    assert body["items"][0]["source"] == "kaggle"
    # A chave interna de dedup nunca vaza no contrato público.
    assert "name_normalized" not in body["items"][0]


def test_list_fighters_filtra_por_nome(client: TestClient, db_session: Session) -> None:
    """``?name=`` filtra case-insensitive e o ``total`` reflete o filtro."""
    _seed(db_session, "Alexander Volkanovski")
    _seed(db_session, "Alex Pereira")
    _seed(db_session, "Jon Jones")

    body = client.get("/api/v1/fighters", params={"name": "alex"}).json()

    assert body["total"] == 2
    assert {item["name"] for item in body["items"]} == {
        "Alexander Volkanovski",
        "Alex Pereira",
    }


def test_list_fighters_pagina_com_limit_e_offset(client: TestClient, db_session: Session) -> None:
    """``limit``/``offset`` paginam sobre a ordem estável por ``name``."""
    for name in ("Charlie", "Alpha", "Bravo", "Delta"):
        _seed(db_session, name)

    body = client.get("/api/v1/fighters", params={"limit": 2, "offset": 2}).json()

    assert body["total"] == 4
    assert body["limit"] == 2
    assert body["offset"] == 2
    assert [item["name"] for item in body["items"]] == ["Charlie", "Delta"]


def test_list_fighters_offset_alem_do_total_devolve_vazio(
    client: TestClient, db_session: Session
) -> None:
    """``offset`` além do total devolve página vazia, mas ``total`` cheio."""
    _seed(db_session, "Jon Jones")

    body = client.get("/api/v1/fighters", params={"offset": 10}).json()

    assert body["items"] == []
    assert body["total"] == 1


def test_list_fighters_limit_acima_do_teto_retorna_422(client: TestClient) -> None:
    """``limit`` acima do teto (100) é rejeitado com 422 pela validação de query."""
    resposta = client.get("/api/v1/fighters", params={"limit": 101})

    assert resposta.status_code == 422


def test_get_fighter_detalha(client: TestClient, db_session: Session) -> None:
    """O detalhe devolve o fighter pelo id com ``source`` no corpo."""
    fighter_id = _seed(db_session, "Israel Adesanya")

    resposta = client.get(f"/api/v1/fighters/{fighter_id}")

    assert resposta.status_code == 200
    body = resposta.json()
    assert body["id"] == fighter_id
    assert body["name"] == "Israel Adesanya"
    assert body["source"] == "kaggle"


def test_get_fighter_inexistente_retorna_404(client: TestClient) -> None:
    """Id inexistente responde 404."""
    resposta = client.get("/api/v1/fighters/999999")

    assert resposta.status_code == 404


def test_get_fighter_id_nao_inteiro_retorna_422(client: TestClient) -> None:
    """Id não-inteiro é rejeitado com 422 pela validação de path."""
    resposta = client.get("/api/v1/fighters/nao-inteiro")

    assert resposta.status_code == 422


def test_fighter_history_ordem_cronologica_com_stats_e_won(
    client: TestClient, db_session: Session
) -> None:
    """Histórico sai em ordem cronológica, cada item com stats do canto e flag ``won``."""
    fighter_id = _seed_history(db_session)

    resposta = client.get(f"/api/v1/fighters/{fighter_id}/bouts")

    assert resposta.status_code == 200
    body = resposta.json()
    assert [item["event_name"] for item in body] == ["UFC 236", "UFC 300"]
    assert [item["event_date"] for item in body] == ["2019-04-13", "2024-04-13"]
    # ``won`` deriva de ``winner_id == fighter_id`` (derrota no antigo, vitória no recente).
    assert [item["won"] for item in body] == [False, True]
    assert body[1]["method"] == "ko_tko"
    # Stats granulares do canto consultado (não do oponente), com ``source`` (RF-09).
    assert body[0]["stats"]["fighter_id"] == fighter_id
    assert body[0]["stats"]["sig_strikes_landed"] == 20
    assert body[0]["stats"]["source"] == "kaggle"
    assert body[1]["stats"]["sig_strikes_landed"] == 50


def test_fighter_history_inexistente_retorna_404(client: TestClient) -> None:
    """Lutador inexistente responde 404 de negócio (``detail`` distinto do 404 de rota)."""
    resposta = client.get("/api/v1/fighters/999999/bouts")

    assert resposta.status_code == 404
    assert resposta.json()["detail"] == "Fighter não encontrado"


def test_fighter_history_sem_lutas_retorna_200_vazio(
    client: TestClient, db_session: Session
) -> None:
    """Lutador existente sem lutas responde 200 com lista vazia (distinto do 404)."""
    fighter_id = _seed(db_session, "Ilia Topuria")

    resposta = client.get(f"/api/v1/fighters/{fighter_id}/bouts")

    assert resposta.status_code == 200
    assert resposta.json() == []


def _seed_stats(db_session: Session) -> int:
    """Semeia duas lutas vencidas do lutador com stats conhecidas; devolve o id.

    Luta 1 (ko_tko): sig 10, td 2, ctrl 60. Luta 2 (decision): sig 20, td 4,
    ctrl 120. Médias esperadas: sig 15, td 3, ctrl 90; vitórias por método
    ``{"ko_tko": 1, "decision": 1}``.
    """
    fighter = FighterFactory.build(name="Alexander Volkanovski")
    db_session.add(fighter)
    db_session.flush()

    for method, sig, td, ctrl in (
        (BoutMethod.KO_TKO, 10, 2, 60),
        (BoutMethod.DECISION, 20, 4, 120),
    ):
        event = EventFactory.build(date=date(2023, 1, 1))
        db_session.add(event)
        db_session.flush()
        bout = BoutFactory.build(event_id=event.id, winner_id=fighter.id, method=method)
        db_session.add(bout)
        db_session.flush()
        db_session.add(
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=fighter.id,
                corner=Corner.RED,
                sig_strikes_landed=sig,
                takedowns_landed=td,
                control_time_seconds=ctrl,
            )
        )
    db_session.flush()
    return fighter.id


def test_openapi_inclui_path_de_stats(client: TestClient) -> None:
    """O contrato OpenAPI expõe a rota de stats resumidas do lutador."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/fighters/{fighter_id}/stats" in paths


def test_fighter_stats_retorna_agregado_on_demand(client: TestClient, db_session: Session) -> None:
    """``/stats`` responde 200 com médias e vitórias por método computadas on demand."""
    fighter_id = _seed_stats(db_session)

    resposta = client.get(f"/api/v1/fighters/{fighter_id}/stats")

    assert resposta.status_code == 200
    body = resposta.json()
    assert body == {
        "fighter_id": fighter_id,
        "bouts_counted": 2,
        "avg_sig_strikes_landed": 15.0,
        "avg_takedowns_landed": 3.0,
        "avg_control_time_seconds": 90.0,
        "wins_by_method": {"ko_tko": 1, "decision": 1},
    }
    # O agregado computado não expõe ``source`` (mistura origens -- decisão do plano).
    assert "source" not in body


def test_fighter_stats_sem_lutas_retorna_medias_nulas(
    client: TestClient, db_session: Session
) -> None:
    """Lutador existente sem lutas responde 200 com médias ``None`` e contagem zero."""
    fighter_id = _seed(db_session, "Ilia Topuria")

    resposta = client.get(f"/api/v1/fighters/{fighter_id}/stats")

    assert resposta.status_code == 200
    body = resposta.json()
    assert body["bouts_counted"] == 0
    assert body["avg_sig_strikes_landed"] is None
    assert body["avg_takedowns_landed"] is None
    assert body["avg_control_time_seconds"] is None
    assert body["wins_by_method"] == {}


def test_fighter_stats_inexistente_retorna_404(client: TestClient) -> None:
    """Lutador inexistente responde 404 de negócio."""
    resposta = client.get("/api/v1/fighters/999999/stats")

    assert resposta.status_code == 404
    assert resposta.json()["detail"] == "Fighter não encontrado"


def test_api_v1_expoe_somente_verbos_get() -> None:
    """Nenhuma rota sob ``/api/v1`` expõe verbo de escrita (API read-only, CA-09)."""
    app = create_app()

    for route in app.routes:
        path: str = getattr(route, "path", "")
        methods: set[str] = getattr(route, "methods", None) or set()
        if path.startswith("/api/v1"):
            assert not (methods & _MUTATING_VERBS), (path, methods)
