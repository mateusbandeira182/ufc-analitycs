"""Testes de API da Slice 01: esqueleto FastAPI + list/detail de fighters.

Exercitam o router fino sobre o Postgres de teste transacional (fixture ``client``,
com ``get_session`` sobreposto). Cobrem o contrato OpenAPI, o envelope de paginação,
a busca por nome ou apelido, o detalhe com 404 e a natureza somente-leitura da API v1.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.features.models import FeaturePayload, FighterAsOfFeatures
from apps.features.selectors import CURRENT_FORM_FEATURES
from apps.fighters.tests.factories import FighterFactory
from ingestion.features.asof import build_asof_materialization, materialize_asof_features
from mma_analytics.app import create_app
from tests.analysis.test_predict import _seed_history as _seed_asof_history

# ``as_of`` fixo da materialização nos testes de FORMA ATUAL: o histórico sintético de
# ``tests.analysis.test_predict`` termina em 2019-2020, e fixar a data mantém o recorte
# "luta anterior" determinístico, sem depender do relógio. Mesmo valor de
# ``apps/features/tests/test_selectors.py``.
_AS_OF = date(2026, 1, 1)

_MUTATING_VERBS = {"POST", "PUT", "PATCH", "DELETE"}


def _seed(db_session: Session, name: str, *, nickname: str | None = None) -> int:
    """Persiste um fighter com o ``name``/``nickname`` dados e devolve o id gerado.

    ``nickname`` é explícito (nulo por padrão) para que os testes de curinga controlem o
    acervo: um apelido gerado ao acaso poderia conter o caractere sob prova.
    """
    fighter = FighterFactory.build(name=name, nickname=nickname)
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
                head_landed=None,
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
                head_landed=30,
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


def test_list_fighters_busca_por_nome(client: TestClient, db_session: Session) -> None:
    """``?q=`` filtra o nome case-insensitive e o ``total`` reflete o filtro."""
    _seed(db_session, "Alexander Volkanovski")
    _seed(db_session, "Alex Pereira")
    _seed(db_session, "Jon Jones")

    body = client.get("/api/v1/fighters", params={"q": "alex"}).json()

    assert body["total"] == 2
    assert {item["name"] for item in body["items"]} == {
        "Alexander Volkanovski",
        "Alex Pereira",
    }


def test_list_fighters_busca_pelo_apelido(client: TestClient, db_session: Session) -> None:
    """CA-17: ``?q=Bones`` devolve Jon Jones, cujo nome não contém o termo buscado."""
    _seed(db_session, "Jon Jones", nickname="Bones")
    _seed(db_session, "Alex Pereira", nickname="Poatan")

    body = client.get("/api/v1/fighters", params={"q": "Bones"}).json()

    assert body["total"] == 1
    (item,) = body["items"]
    assert item["name"] == "Jon Jones"
    assert item["nickname"] == "Bones"


def test_list_fighters_curinga_vale_como_texto_literal(
    client: TestClient, db_session: Session
) -> None:
    """CA-26a: ``?q=%`` num acervo sem ``%`` devolve zero itens -- não o acervo inteiro."""
    _seed(db_session, "Jon Jones", nickname="Bones")
    _seed(db_session, "Alex Pereira", nickname="Poatan")

    body = client.get("/api/v1/fighters", params={"q": "%"}).json()

    assert body["items"] == []
    assert body["total"] == 0


def test_list_fighters_ignora_o_parametro_name_aposentado(
    client: TestClient, db_session: Session
) -> None:
    """D-02: ``name`` saiu do contrato sem alias, então ``?name=`` não filtra nada.

    A quebra é deliberada: o FastAPI ignora query param desconhecido, e a SPA -- único
    consumidor -- é reescrita nesta mesma SPEC.
    """
    _seed(db_session, "Jon Jones")
    _seed(db_session, "Alex Pereira")

    body = client.get("/api/v1/fighters", params={"name": "Jon"}).json()

    assert body["total"] == 2
    assert {item["name"] for item in body["items"]} == {"Jon Jones", "Alex Pereira"}


def test_openapi_lista_de_fighters_expoe_q_e_nao_name(client: TestClient) -> None:
    """O contrato publica ``q`` e aposenta ``name`` -- sem alias (D-02)."""
    operacao = client.get("/openapi.json").json()["paths"]["/api/v1/fighters"]["get"]

    nomes = {parametro["name"] for parametro in operacao["parameters"]}
    assert "q" in nomes
    assert "name" not in nomes


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


def test_get_fighter_expoe_weight_kg(client: TestClient, db_session: Session) -> None:
    """O detalhe expõe ``weight_kg`` (M5, nullable) quando preenchido."""
    fighter = FighterFactory.build(name="Jon Jones", weight_kg=93.0)
    db_session.add(fighter)
    db_session.flush()

    body = client.get(f"/api/v1/fighters/{fighter.id}").json()

    assert body["weight_kg"] == 93.0


def test_get_fighter_weight_kg_ausente_sai_nulo(client: TestClient, db_session: Session) -> None:
    """Lutador sem peso registrado expõe ``weight_kg`` nulo (aditivo/nullable)."""
    fighter = FighterFactory.build(name="Royce Gracie", weight_kg=None)
    db_session.add(fighter)
    db_session.flush()

    body = client.get(f"/api/v1/fighters/{fighter.id}").json()

    assert body["weight_kg"] is None


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
    assert body[0]["stats"]["name"] == "Max Holloway"  # identidade do próprio canto
    assert body[0]["stats"]["sig_strikes_landed"] == 20
    assert body[0]["stats"]["source"] == "kaggle"
    assert body[1]["stats"]["sig_strikes_landed"] == 50
    # Splits do M5 fluem pelo mesmo schema no histórico (nulos quando ausentes).
    assert body[1]["stats"]["head_landed"] == 30
    assert body[0]["stats"]["head_landed"] is None
    # Adversário daquela luta (o outro canto); nulo quando não houver segundo canto.
    assert body[0]["opponent"]["name"] == "Dustin Poirier"
    assert body[1]["opponent"] is None


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
    ``{"ko_tko": 1, "decision": 1}``. Os splits são somados na carreira para o
    perfil de striking: alvo head=20/body=10/leg=10 (shares 0.5/0.25/0.25);
    posição distância=30/clinch=5/solo=5 (shares 0.75/0.125/0.125).
    """
    fighter = FighterFactory.build(name="Alexander Volkanovski")
    db_session.add(fighter)
    db_session.flush()

    for method, sig, td, ctrl, splits in (
        (BoutMethod.KO_TKO, 10, 2, 60, (15, 5, 0, 20, 0, 0)),
        (BoutMethod.DECISION, 20, 4, 120, (5, 5, 10, 10, 5, 5)),
    ):
        head, body, leg, distance, clinch, ground = splits
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
                head_landed=head,
                body_landed=body,
                leg_landed=leg,
                distance_landed=distance,
                clinch_landed=clinch,
                ground_landed=ground,
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
        "striking_profile": {
            "share_head": 0.5,
            "share_body": 0.25,
            "share_leg": 0.25,
            "share_distance": 0.75,
            "share_clinch": 0.125,
            "share_ground": 0.125,
        },
        # Bloco as-of da seção FORMA ATUAL (SPEC 011, RF-15), nulo porque o cache nunca foi
        # materializado -- ausência declarada, nunca treze zeros. A asserção é de **corpo
        # inteiro** de propósito: campo novo no contrato quebra aqui, em vez de entrar calado.
        "current_form": None,
        # Diagnóstico do cache as-of (SPEC 011, D-09). Aqui o cache nunca foi materializado:
        # o agregado não depende dele, então a resposta segue 200 e o campo reporta o estado.
        "features_freshness": {
            "materialized_at": None,
            "n_bouts_source": 0,
            "n_bouts_current": 2,
            "stale": True,
        },
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
    # Sem lutas, o perfil de striking sai todo nulo (denominador zero -> null).
    assert body["striking_profile"] == {
        "share_head": None,
        "share_body": None,
        "share_leg": None,
        "share_distance": None,
        "share_clinch": None,
        "share_ground": None,
    }


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


def test_get_fighter_expoe_as_duas_urls_de_imagem(client: TestClient, db_session: Session) -> None:
    """CA-08: o detalhe devolve ``headshot_url`` e ``body_image_url`` exatas quando preenchidas."""
    fighter = FighterFactory.build(
        name="Umar Nurmagomedov",
        headshot_url="https://ufc.com/images/styles/event_results_athlete_headshot/s3/a.png",
        body_image_url="https://ufc.com/images/styles/athlete_bio_full_body/s3/b.png",
    )
    db_session.add(fighter)
    db_session.flush()

    body = client.get(f"/api/v1/fighters/{fighter.id}").json()

    assert (
        body["headshot_url"]
        == "https://ufc.com/images/styles/event_results_athlete_headshot/s3/a.png"
    )
    assert body["body_image_url"] == "https://ufc.com/images/styles/athlete_bio_full_body/s3/b.png"


def test_get_fighter_sem_imagem_sai_com_as_duas_chaves_nulas(
    client: TestClient, db_session: Session
) -> None:
    """CA-08: sem imagem, as duas chaves estão **presentes** com ``null`` -- nunca ausentes."""
    fighter = FighterFactory.build(name="Royce Gracie", headshot_url=None, body_image_url=None)
    db_session.add(fighter)
    db_session.flush()

    body = client.get(f"/api/v1/fighters/{fighter.id}").json()

    assert "headshot_url" in body
    assert "body_image_url" in body
    assert body["headshot_url"] is None
    assert body["body_image_url"] is None


def test_get_fighter_com_so_o_retrato_deixa_o_corpo_nulo(
    client: TestClient, db_session: Session
) -> None:
    """CA-08: só o retrato persistido -> ``body_image_url`` nulo (nunca string vazia)."""
    fighter = FighterFactory.build(
        name="Kai Asakura",
        headshot_url="https://ufc.com/images/styles/event_results_athlete_headshot/s3/c.png",
        body_image_url=None,
    )
    db_session.add(fighter)
    db_session.flush()

    body = client.get(f"/api/v1/fighters/{fighter.id}").json()

    assert body["headshot_url"].endswith("c.png")
    assert body["body_image_url"] is None


def test_listagem_expoe_as_urls_de_imagem_pelo_mesmo_schema(
    client: TestClient, db_session: Session
) -> None:
    """CA-08: a listagem reusa ``FighterOut`` e passa a expor os dois campos, a custo zero."""
    fighter = FighterFactory.build(
        name="Song Yadong",
        headshot_url="https://ufc.com/images/styles/event_results_athlete_headshot/s3/d.png",
        body_image_url=None,
    )
    db_session.add(fighter)
    db_session.flush()

    body = client.get("/api/v1/fighters", params={"q": "Song Yadong"}).json()

    (item,) = body["items"]
    assert item["headshot_url"].endswith("d.png")
    assert item["body_image_url"] is None


def test_fighter_stats_traz_o_diagnostico_de_defasagem(
    client: TestClient, db_session: Session
) -> None:
    """CA-04: ``/fighters/{id}/stats`` devolve ``features_freshness`` com os quatro campos.

    Terceiro (e último) endpoint que lê o cache as-of, por D-09. Aqui o diagnóstico vale
    mesmo **sem** cache materializado: o agregado on demand não depende dele, então a
    resposta segue 200 e ``materialized_at`` volta nulo (emenda DP-05 do Plano 011-01).
    """
    fighter_id = _seed_stats(db_session)

    body = client.get(f"/api/v1/fighters/{fighter_id}/stats").json()

    assert set(body["features_freshness"]) == {
        "materialized_at",
        "n_bouts_source",
        "n_bouts_current",
        "stale",
    }
    assert body["features_freshness"]["materialized_at"] is None
    assert body["features_freshness"]["n_bouts_source"] == 0
    assert body["features_freshness"]["n_bouts_current"] == 2
    assert body["features_freshness"]["stale"] is True


def _materializar_asof(db_session: Session) -> dict[str, int]:
    """Semeia o histórico sintético, materializa o cache as-of e devolve nome -> id.

    Reusa a pipeline **real** (``ingestion.features.asof``) em vez de inserir linhas à mão:
    é o que faz o teste de API provar que os treze nomes projetados existem no payload que a
    produção grava, e não apenas num payload que o próprio teste inventou.

    O nome vem aliasado no import porque este módulo já tem um ``_seed_history`` próprio, com
    outro propósito (duas lutas para o teste de histórico, sem passar pela pipeline).
    """
    ids = _seed_asof_history(db_session)
    materialize_asof_features(db_session, build_asof_materialization(db_session, _AS_OF))
    return ids


def test_fighter_stats_traz_current_form_ao_lado_do_diagnostico(
    client: TestClient, db_session: Session
) -> None:
    """CA-01: ``/stats`` devolve ``current_form`` com os treze campos as-of (RF-15).

    O bloco é **próprio** na resposta, nunca campos soltos misturados ao agregado de
    carreira: CARREIRA e FORMA ATUAL respondem perguntas diferentes (tudo contra as três
    últimas lutas), e misturá-las é o defeito que a RF-24 existe para evitar.
    """
    ids = _materializar_asof(db_session)

    resposta = client.get(f"/api/v1/fighters/{ids['Fighter Alpha']}/stats")

    assert resposta.status_code == 200
    body = resposta.json()
    assert set(body["current_form"]) == set(CURRENT_FORM_FEATURES)
    # Lido da MESMA tabela que alimenta a predição -- os dois contam a mesma história.
    assert body["features_freshness"]["materialized_at"] is not None
    assert body["features_freshness"]["stale"] is False
    # Os valores chegam como número, não como string do JSONB.
    assert isinstance(body["current_form"]["career_minutes_before"], float)


def test_fighter_stats_sem_linha_as_of_responde_200_com_current_form_nulo(
    client: TestClient, db_session: Session
) -> None:
    """CA-02: lutador sem linha no cache responde **200** com ``current_form: null``.

    Nunca 404 (o agregado de carreira não depende do cache) e nunca um vetor de zeros: o
    estreante não tem forma recente a mostrar, e zero em golpe significativo seria uma
    afirmação sobre ele. Aqui o cache está **em dia** -- a ausência é do lutador, não do
    cache, e é por isso que o caso difere do CA-03.
    """
    _materializar_asof(db_session)
    estreante_id = _seed(db_session, "Ilia Topuria")

    resposta = client.get(f"/api/v1/fighters/{estreante_id}/stats")

    assert resposta.status_code == 200
    body = resposta.json()
    assert body["current_form"] is None
    assert body["features_freshness"]["materialized_at"] is not None


def test_fighter_stats_com_cache_nunca_materializado(
    client: TestClient, db_session: Session
) -> None:
    """CA-03: cache nunca materializado -- ``current_form`` nulo e o diagnóstico explica.

    As duas ausências se distinguem na mesma resposta: ``current_form: null`` com
    ``materialized_at: null`` e ``stale: true`` é "ninguém materializou o cache"; com
    ``materialized_at`` preenchido seria "este lutador não tem histórico". Honra a emenda
    DP-05 (``materialized_at`` nulável).
    """
    fighter_id = _seed_stats(db_session)

    body = client.get(f"/api/v1/fighters/{fighter_id}/stats").json()

    assert body["current_form"] is None
    assert body["features_freshness"]["materialized_at"] is None
    assert body["features_freshness"]["n_bouts_source"] == 0
    assert body["features_freshness"]["stale"] is True


def test_fighter_stats_feature_nula_chega_ao_json_como_null(
    client: TestClient, db_session: Session
) -> None:
    """CA-04: valor nulo no payload sai ``null`` no JSON, jamais ``0``.

    A asserção é ``is None`` de propósito: ``assert not body[...]`` passaria com ``0`` e
    afirmaria exatamente o contrário do que este teste existe para provar. O zero gravado
    convive no mesmo payload para que a distinção seja medida, não presumida.
    """
    fighter_id = _seed_stats(db_session)
    payload: FeaturePayload = dict.fromkeys(CURRENT_FORM_FEATURES, 1.5)
    payload["control_time_avg_r3"] = None
    payload["submission_losses_prior"] = 0
    db_session.add(
        FighterAsOfFeatures(
            fighter_id=fighter_id,
            features=payload,
            style_history=[],
            n_bouts_source=2,
            source="test",
            materialized_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    body = client.get(f"/api/v1/fighters/{fighter_id}/stats").json()

    assert body["current_form"]["control_time_avg_r3"] is None
    assert body["current_form"]["submission_losses_prior"] == 0.0
