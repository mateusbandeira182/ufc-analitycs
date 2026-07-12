"""Testes de API da Slice 02: list/detail de events com o card de bouts.

Exercitam o router fino sobre o Postgres de teste transacional (fixture ``client``,
com ``get_session`` sobreposto). Cobrem o contrato OpenAPI, o envelope de paginação,
a ordem recentes-primeiro, o detalhe com o card, o event sem bouts e o 404.
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.events.models import Event
from apps.fighters.tests.factories import FighterFactory


def _seed_event(db_session: Session, name: str, event_date: date) -> Event:
    """Persiste um event e devolve o model (com id gerado)."""
    event = EventFactory.build(name=name, date=event_date)
    db_session.add(event)
    db_session.flush()
    return event


def test_openapi_inclui_paths_de_events(client: TestClient) -> None:
    """O contrato OpenAPI expõe as rotas de events sob ``/api/v1``."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/events" in paths
    assert "/api/v1/events/{event_id}" in paths


def test_list_events_retorna_envelope_page_com_source(
    client: TestClient, db_session: Session
) -> None:
    """A lista devolve o envelope ``Page`` e cada item traz ``source`` (RF-09)."""
    _seed_event(db_session, "UFC 300", date(2024, 4, 13))

    body = client.get("/api/v1/events").json()

    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["items"][0]["name"] == "UFC 300"
    assert body["items"][0]["date"] == "2024-04-13"
    assert body["items"][0]["source"] == "kaggle"


def test_list_events_ordena_recentes_primeiro(client: TestClient, db_session: Session) -> None:
    """A lista devolve os events mais recentes primeiro (data decrescente)."""
    _seed_event(db_session, "UFC 290", date(2023, 7, 8))
    _seed_event(db_session, "UFC 310", date(2024, 12, 7))
    _seed_event(db_session, "UFC 300", date(2024, 4, 13))

    body = client.get("/api/v1/events").json()

    assert [item["name"] for item in body["items"]] == ["UFC 310", "UFC 300", "UFC 290"]


def test_list_events_pagina_com_limit_e_offset(client: TestClient, db_session: Session) -> None:
    """``limit``/``offset`` paginam sobre a ordem recentes-primeiro; ``total`` é geral."""
    _seed_event(db_session, "UFC 290", date(2023, 7, 8))
    _seed_event(db_session, "UFC 310", date(2024, 12, 7))
    _seed_event(db_session, "UFC 300", date(2024, 4, 13))

    body = client.get("/api/v1/events", params={"limit": 2, "offset": 2}).json()

    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 2
    assert [item["name"] for item in body["items"]] == ["UFC 290"]


def test_list_events_offset_alem_do_total_devolve_vazio(
    client: TestClient, db_session: Session
) -> None:
    """``offset`` além do total devolve página vazia, mas ``total`` cheio."""
    _seed_event(db_session, "UFC 300", date(2024, 4, 13))

    body = client.get("/api/v1/events", params={"offset": 10}).json()

    assert body["items"] == []
    assert body["total"] == 1


def test_get_event_detalha_com_card_de_bouts(client: TestClient, db_session: Session) -> None:
    """O detalhe traz os campos do event e o card de bouts (sem stats granulares)."""
    event = _seed_event(db_session, "UFC 300", date(2024, 4, 13))
    red = FighterFactory.build(name="Alex Pereira")
    blue = FighterFactory.build(name="Jamahal Hill")
    db_session.add_all([red, blue])
    db_session.flush()
    bout = BoutFactory.build(
        event_id=event.id,
        winner_id=red.id,
        method=BoutMethod.KO_TKO,
        round=2,
        ending_time_seconds=143,
        weight_class="Lightweight",
    )
    db_session.add(bout)
    db_session.flush()
    db_session.add_all(
        [
            BoutFighterFactory.build(bout_id=bout.id, fighter_id=red.id, corner=Corner.RED),
            BoutFighterFactory.build(bout_id=bout.id, fighter_id=blue.id, corner=Corner.BLUE),
        ]
    )
    db_session.flush()

    resposta = client.get(f"/api/v1/events/{event.id}")

    assert resposta.status_code == 200
    body = resposta.json()
    assert body["id"] == event.id
    assert body["name"] == "UFC 300"
    assert body["source"] == "kaggle"
    assert len(body["bouts"]) == 1
    card = body["bouts"][0]
    assert card["id"] == bout.id
    assert card["winner_id"] == red.id
    assert card["method"] == "ko_tko"
    assert card["round"] == 2
    assert card["ending_time_seconds"] == 143
    assert card["weight_class"] == "Lightweight"
    assert card["source"] == "kaggle"
    # A dupla de participantes aparece no card (enrich SPA): id, nome e canto.
    por_canto = {f["corner"]: f for f in card["fighters"]}
    assert set(por_canto) == {Corner.RED.value, Corner.BLUE.value}
    assert por_canto[Corner.RED.value]["fighter_id"] == red.id
    assert por_canto[Corner.RED.value]["name"] == "Alex Pereira"
    assert por_canto[Corner.BLUE.value]["name"] == "Jamahal Hill"
    # O card não vaza stats granulares de bout_fighters (essas ficam no detalhe da luta).
    assert "sig_strikes_landed" not in card
    assert "corner" not in card  # ``corner`` mora em cada participante, não no card


def test_get_event_sem_bouts_devolve_card_vazio(client: TestClient, db_session: Session) -> None:
    """Event sem bouts responde 200 com ``bouts: []``."""
    event = _seed_event(db_session, "UFC 300", date(2024, 4, 13))

    resposta = client.get(f"/api/v1/events/{event.id}")

    assert resposta.status_code == 200
    assert resposta.json()["bouts"] == []


def test_get_event_inexistente_retorna_404(client: TestClient) -> None:
    """Id inexistente responde 404."""
    resposta = client.get("/api/v1/events/999999")

    assert resposta.status_code == 404


def test_get_event_id_nao_inteiro_retorna_422(client: TestClient) -> None:
    """Id não-inteiro é rejeitado com 422 pela validação de path."""
    resposta = client.get("/api/v1/events/nao-inteiro")

    assert resposta.status_code == 422
