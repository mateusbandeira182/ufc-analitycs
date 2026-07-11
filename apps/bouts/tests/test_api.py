"""Testes de API da Slice 03: ``GET /api/v1/bouts/{id}`` com stats granulares.

Exercitam o router fino sobre o Postgres de teste transacional (fixture
``client``, com ``get_session`` sobreposto). Cobrem o contrato OpenAPI, o detalhe
com os dois cantos distintos e as stats granulares por luta (nunca médias), o
``source`` no response, e o 404 para id inexistente.
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.fighters.models import Fighter
from apps.fighters.tests.factories import FighterFactory


def _seed_bout(db_session: Session) -> Bout:
    """Semeia 1 evento, 2 lutadores, 1 luta e os 2 cantos com stats distintas."""
    event = EventFactory.build(name="UFC 300", date=date(2024, 4, 13))
    red = FighterFactory.build(name="Alex Pereira")
    blue = FighterFactory.build(name="Jamahal Hill")
    db_session.add_all([event, red, blue])
    db_session.flush()

    bout = BoutFactory.build(
        event_id=event.id,
        winner_id=red.id,
        method=BoutMethod.KO_TKO,
        round=1,
        ending_time_seconds=191,
        weight_class="Light Heavyweight",
    )
    db_session.add(bout)
    db_session.flush()

    db_session.add_all(
        [
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=red.id,
                corner=Corner.RED,
                knockdowns=1,
                sig_strikes_landed=40,
                sig_strikes_attempted=55,
                takedowns_landed=0,
                takedowns_attempted=0,
                submission_attempts=0,
                control_time_seconds=90,
            ),
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=blue.id,
                corner=Corner.BLUE,
                knockdowns=0,
                sig_strikes_landed=12,
                sig_strikes_attempted=30,
                takedowns_landed=1,
                takedowns_attempted=3,
                submission_attempts=0,
                control_time_seconds=30,
            ),
        ]
    )
    db_session.flush()
    return bout


def test_openapi_inclui_path_de_bouts(client: TestClient) -> None:
    """O contrato OpenAPI expõe a rota de detalhe de bout sob ``/api/v1``."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/bouts/{bout_id}" in paths


def test_get_bout_detalha_com_evento_metodo_e_source(
    client: TestClient, db_session: Session
) -> None:
    """O detalhe traz evento, vencedor, método, round, tempo e ``source`` (RF-09)."""
    bout = _seed_bout(db_session)

    resposta = client.get(f"/api/v1/bouts/{bout.id}")

    assert resposta.status_code == 200
    body = resposta.json()
    assert body["id"] == bout.id
    assert body["event"]["name"] == "UFC 300"
    assert body["event"]["date"] == "2024-04-13"
    assert body["event"]["source"] == "kaggle"
    assert body["winner_id"] == bout.winner_id
    assert body["method"] == BoutMethod.KO_TKO.value
    assert body["round"] == 1
    assert body["ending_time_seconds"] == 191
    assert body["weight_class"] == "Light Heavyweight"
    assert body["source"] == "kaggle"


def test_get_bout_expoe_stats_granulares_dos_dois_cantos(
    client: TestClient, db_session: Session
) -> None:
    """Os dois cantos aparecem distintos, com as stats por luta como gravadas."""
    bout = _seed_bout(db_session)

    body = client.get(f"/api/v1/bouts/{bout.id}").json()

    fighters = body["fighters"]
    assert len(fighters) == 2
    por_canto = {bf["corner"]: bf for bf in fighters}
    assert set(por_canto) == {Corner.RED.value, Corner.BLUE.value}

    red = por_canto[Corner.RED.value]
    assert red["knockdowns"] == 1
    assert red["sig_strikes_landed"] == 40
    assert red["sig_strikes_attempted"] == 55
    assert red["control_time_seconds"] == 90
    assert red["source"] == "kaggle"

    blue = por_canto[Corner.BLUE.value]
    assert blue["sig_strikes_landed"] == 12
    assert blue["takedowns_landed"] == 1
    assert blue["takedowns_attempted"] == 3


def test_get_bout_inexistente_retorna_404(client: TestClient) -> None:
    """Id inexistente responde 404."""
    resposta = client.get("/api/v1/bouts/999999")

    assert resposta.status_code == 404


def test_get_bout_id_nao_inteiro_retorna_422(client: TestClient) -> None:
    """Id não-inteiro é rejeitado com 422 pela validação de path."""
    resposta = client.get("/api/v1/bouts/nao-inteiro")

    assert resposta.status_code == 422


# --- Slice 05: head-to-head entre dois lutadores -----------------------------


def _seed_confronto(
    db_session: Session,
    *,
    event_name: str,
    event_date: date,
    red: Fighter,
    blue: Fighter,
    winner: Fighter,
) -> Bout:
    """Semeia 1 evento e 1 luta com os dois cantos entre ``red`` e ``blue``."""
    event = EventFactory.build(name=event_name, date=event_date)
    db_session.add(event)
    db_session.flush()

    bout = BoutFactory.build(
        event_id=event.id,
        winner_id=winner.id,
        method=BoutMethod.DECISION,
        round=5,
        ending_time_seconds=300,
        weight_class="Featherweight",
    )
    db_session.add(bout)
    db_session.flush()

    db_session.add_all(
        [
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=red.id,
                corner=Corner.RED,
                sig_strikes_landed=50,
                control_time_seconds=120,
            ),
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=blue.id,
                corner=Corner.BLUE,
                sig_strikes_landed=45,
                control_time_seconds=60,
            ),
        ]
    )
    db_session.flush()
    return bout


def test_openapi_inclui_path_de_head_to_head(client: TestClient) -> None:
    """O contrato OpenAPI expõe a rota de head-to-head sob ``/api/v1``."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/head-to-head" in paths


def test_head_to_head_retorna_confrontos_em_ordem_com_stats_granulares(
    client: TestClient, db_session: Session
) -> None:
    """200 com envelope ``HeadToHeadOut``: ordem cronológica e stats dos dois cantos."""
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b])
    db_session.flush()
    antiga = _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=b, winner=a
    )
    recente = _seed_confronto(
        db_session, event_name="UFC 276", event_date=date(2022, 7, 2), red=b, blue=a, winner=a
    )

    resposta = client.get("/api/v1/head-to-head", params={"a": a.id, "b": b.id})

    assert resposta.status_code == 200
    body = resposta.json()
    assert body["fighter_a_id"] == a.id
    assert body["fighter_b_id"] == b.id
    assert [bout["id"] for bout in body["bouts"]] == [antiga.id, recente.id]

    primeiro = body["bouts"][0]
    assert primeiro["method"] == BoutMethod.DECISION.value
    assert primeiro["source"] == "kaggle"
    assert len(primeiro["fighters"]) == 2
    por_canto = {bf["corner"]: bf for bf in primeiro["fighters"]}
    assert set(por_canto) == {Corner.RED.value, Corner.BLUE.value}
    assert por_canto[Corner.RED.value]["sig_strikes_landed"] == 50
    assert por_canto[Corner.RED.value]["source"] == "kaggle"


def test_head_to_head_nao_vaza_luta_contra_terceiro(
    client: TestClient, db_session: Session
) -> None:
    """Uma luta A-vs-C não pode aparecer no head-to-head A-vs-B."""
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Max Holloway")
    c = FighterFactory.build(name="Islam Makhachev")
    db_session.add_all([a, b, c])
    db_session.flush()
    ab = _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=b, winner=a
    )
    _seed_confronto(
        db_session, event_name="UFC 284", event_date=date(2023, 2, 11), red=a, blue=c, winner=c
    )

    body = client.get("/api/v1/head-to-head", params={"a": a.id, "b": b.id}).json()

    assert [bout["id"] for bout in body["bouts"]] == [ab.id]


def test_head_to_head_sem_confronto_direto_retorna_200_lista_vazia(
    client: TestClient, db_session: Session
) -> None:
    """Ambos existem mas nunca lutaram: 200 com ``bouts == []`` (não 404)."""
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b])
    db_session.flush()

    resposta = client.get("/api/v1/head-to-head", params={"a": a.id, "b": b.id})

    assert resposta.status_code == 200
    assert resposta.json()["bouts"] == []


def test_head_to_head_mesmo_id_retorna_422(client: TestClient, db_session: Session) -> None:
    """``a == b`` é rejeitado com 422 e mensagem explícita."""
    f = FighterFactory.build(name="Alexander Volkanovski")
    db_session.add(f)
    db_session.flush()

    resposta = client.get("/api/v1/head-to-head", params={"a": f.id, "b": f.id})

    assert resposta.status_code == 422
    assert resposta.json()["detail"] == "a e b devem ser lutadores distintos"


def test_head_to_head_id_inexistente_retorna_404(client: TestClient, db_session: Session) -> None:
    """Um dos lutadores inexistente responde 404 (distinto de lista vazia)."""
    f = FighterFactory.build(name="Alexander Volkanovski")
    db_session.add(f)
    db_session.flush()

    resposta = client.get("/api/v1/head-to-head", params={"a": f.id, "b": 999_999})

    assert resposta.status_code == 404
