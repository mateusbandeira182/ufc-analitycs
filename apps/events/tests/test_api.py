"""Testes de API da Slice 02: list/detail de events com o card de bouts.

Exercitam o router fino sobre o Postgres de teste transacional (fixture ``client``,
com ``get_session`` sobreposto). Cobrem o contrato OpenAPI, o envelope de paginação,
a ordem recentes-primeiro, o detalhe com o card, o event sem bouts e o 404.
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, BoutStatus, Corner
from apps.bouts.models import Bout
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


def test_get_event_nao_traz_diagnostico_de_defasagem(
    client: TestClient, db_session: Session
) -> None:
    """Guarda de D-09: ``/events/{id}`` **não** carrega ``features_freshness``.

    O campo acompanha exatamente os três endpoints que **leem** o cache as-of
    (``/predict/matchup``, ``/predict/event/{id}`` e ``/fighters/{id}/stats``). Eventos não o
    leem e não têm o que reportar; acrescentá-lo aqui seria um aviso sobre um dado que esta
    resposta não usa -- e aviso sem consequência é o que treina o usuário a ignorar avisos.
    """
    event = _seed_event(db_session, "UFC 300", date(2024, 4, 13))

    body = client.get(f"/api/v1/events/{event.id}").json()

    assert "features_freshness" not in body


def test_get_event_com_luta_agendada_devolve_method_nulo(
    client: TestClient, db_session: Session
) -> None:
    """CA-P02: o card de um evento com luta agendada responde 200 e ``method: null``.

    É a prova de que ``method`` nullable chegou íntegro ao contrato servido: sem o widening
    dos schemas de saída, o Pydantic levantaria ``ValidationError`` e a resposta viraria 500
    -- e a página do evento futuro simplesmente não abriria.

    ``status`` passou a ser exposto na **Slice 07**, como este teste previa desde a Slice 02;
    a asserção negativa daquela época virou a positiva ``status == "scheduled"``.
    """
    event = _seed_event(db_session, "UFC Card Futuro", date(2026, 12, 12))
    red = FighterFactory.build(name="Alex Pereira")
    blue = FighterFactory.build(name="Jamahal Hill")
    db_session.add_all([red, blue])
    db_session.flush()
    bout = BoutFactory.build(
        event_id=event.id,
        winner_id=None,
        method=None,
        round=None,
        ending_time_seconds=None,
        status=BoutStatus.SCHEDULED,
        weight_class="Light Heavyweight",
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
    card = resposta.json()["bouts"][0]
    assert card["id"] == bout.id
    assert card["method"] is None
    assert card["winner_id"] is None
    assert card["status"] == BoutStatus.SCHEDULED.value  # nasceu na Slice 07
    assert {f["corner"] for f in card["fighters"]} == {Corner.RED.value, Corner.BLUE.value}


def _seed_bout(db_session: Session, event: Event, *, status: BoutStatus, **campos: object) -> Bout:
    """Persiste uma luta do ``event`` com o ``status`` dado e devolve o model."""
    bout = BoutFactory.build(event_id=event.id, status=status, **campos)
    db_session.add(bout)
    db_session.flush()
    return bout


def test_list_events_when_particiona_o_acervo(client: TestClient, db_session: Session) -> None:
    """``when=past|upcoming`` parte o acervo em dois conjuntos disjuntos que somam o total.

    A partição deriva do ``status`` da luta (DT-1): o evento de data passada cujas lutas
    seguem todas ``scheduled`` é **próximo**, não realizado.
    """
    realizado = _seed_event(db_session, "UFC 300", date(2024, 4, 13))
    _seed_bout(db_session, realizado, status=BoutStatus.COMPLETED)
    agendado = _seed_event(db_session, "UFC FN: Hooker vs Parnasse", date(2026, 9, 5))
    _seed_bout(db_session, agendado, status=BoutStatus.SCHEDULED, winner_id=None, method=None)

    acervo = client.get("/api/v1/events").json()
    passados = client.get("/api/v1/events", params={"when": "past"}).json()
    proximos = client.get("/api/v1/events", params={"when": "upcoming"}).json()

    assert [item["id"] for item in passados["items"]] == [realizado.id]
    assert [item["id"] for item in proximos["items"]] == [agendado.id]
    assert passados["total"] + proximos["total"] == acervo["total"]


def test_list_events_when_total_reflete_o_filtro(client: TestClient, db_session: Session) -> None:
    """O ``total`` da resposta filtrada conta o conjunto da seção, não o acervo inteiro."""
    for indice in range(3):
        realizado = _seed_event(db_session, f"UFC 30{indice}", date(2024, 4, 13 + indice))
        _seed_bout(db_session, realizado, status=BoutStatus.COMPLETED)
    agendado = _seed_event(db_session, "UFC 335", date(2026, 12, 12))
    _seed_bout(db_session, agendado, status=BoutStatus.SCHEDULED, winner_id=None, method=None)

    body = client.get("/api/v1/events", params={"when": "upcoming"}).json()

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [agendado.id]


def test_list_events_when_invalido_responde_422(client: TestClient) -> None:
    """``when`` fora de ``{past, upcoming}`` é rejeitado com 422, não ignorado.

    Sem o ``Literal`` na assinatura, o FastAPI **ignora** o query param desconhecido e
    devolve o acervo inteiro -- a ocorrência nº 3 do padrão desta SPEC, em que o defeito
    se parece com uma resposta de sucesso.
    """
    resposta = client.get("/api/v1/events", params={"when": "futuro"})

    assert resposta.status_code == 422


def test_list_events_sem_when_nao_filtra(client: TestClient, db_session: Session) -> None:
    """Sem ``when``, a lista traz o acervo inteiro na ordem decrescente de hoje."""
    agendado = _seed_event(db_session, "UFC 335", date(2026, 12, 12))
    _seed_bout(db_session, agendado, status=BoutStatus.SCHEDULED, winner_id=None, method=None)
    realizado = _seed_event(db_session, "UFC 300", date(2024, 4, 13))
    _seed_bout(db_session, realizado, status=BoutStatus.COMPLETED)

    body = client.get("/api/v1/events").json()

    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [agendado.id, realizado.id]


def _seed_card_realizado(db_session: Session, event: Event) -> dict[str, int]:
    """Semeia um card realizado com dois desfechos e devolve os ids relevantes.

    Uma finalização de R1 e uma decisão de R3: base suficiente para ``summary`` contar por
    método e para ``highlights`` ter finalização mais rápida e destaques de volume.
    """
    red = FighterFactory.build(name="Alex Pereira", headshot_url="https://ufc.test/pereira.png")
    blue = FighterFactory.build(name="Jamahal Hill", headshot_url=None)
    db_session.add_all([red, blue])
    db_session.flush()
    finalizacao = BoutFactory.build(
        event_id=event.id,
        winner_id=red.id,
        method=BoutMethod.KO_TKO,
        round=1,
        ending_time_seconds=143,
        weight_class="Light Heavyweight",
        card_section="Main Card",
        bout_order=1001,
        status=BoutStatus.COMPLETED,
    )
    decisao = BoutFactory.build(
        event_id=event.id,
        winner_id=blue.id,
        method=BoutMethod.DECISION,
        round=3,
        ending_time_seconds=300,
        weight_class="Lightweight",
        card_section="Prelims",
        bout_order=2001,
        status=BoutStatus.COMPLETED,
    )
    db_session.add_all([finalizacao, decisao])
    db_session.flush()
    for bout in (finalizacao, decisao):
        db_session.add_all(
            [
                BoutFighterFactory.build(
                    bout_id=bout.id,
                    fighter_id=red.id,
                    corner=Corner.RED,
                    sig_strikes_landed=42,
                    takedowns_landed=2,
                ),
                BoutFighterFactory.build(
                    bout_id=bout.id,
                    fighter_id=blue.id,
                    corner=Corner.BLUE,
                    sig_strikes_landed=10,
                    takedowns_landed=0,
                ),
            ]
        )
    db_session.flush()
    return {
        "red": red.id,
        "blue": blue.id,
        "finalizacao": finalizacao.id,
        "decisao": decisao.id,
    }


def test_openapi_inclui_a_rota_de_latest(client: TestClient) -> None:
    """O contrato OpenAPI expõe ``/api/v1/events/latest`` como rota própria."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/events/latest" in paths


def test_get_events_latest_responde_200(client: TestClient, db_session: Session) -> None:
    """``GET /events/latest`` responde **200** com o último evento realizado.

    O 200 é a asserção, não a ausência de erro: se ``/latest`` fosse declarada **depois**
    de ``/{event_id}``, o FastAPI casaria ``event_id="latest"`` e devolveria 422 -- um erro
    silenciosamente plausível ("id inválido").
    """
    antigo = _seed_event(db_session, "UFC 290", date(2023, 7, 8))
    _seed_card_realizado(db_session, antigo)
    recente = _seed_event(db_session, "UFC 300", date(2024, 4, 13))
    _seed_card_realizado(db_session, recente)
    futuro = _seed_event(db_session, "UFC 335", date(2026, 12, 12))
    _seed_bout(db_session, futuro, status=BoutStatus.SCHEDULED, winner_id=None, method=None)

    resposta = client.get("/api/v1/events/latest")

    assert resposta.status_code == 200
    body = resposta.json()
    assert body["id"] == recente.id
    assert body["name"] == "UFC 300"
    assert len(body["bouts"]) == 2
    assert body["summary"]["n_bouts"] == 2
    assert body["highlights"]["fastest_finish"]["round"] == 1


def test_get_events_latest_sem_evento_realizado_responde_404(client: TestClient) -> None:
    """Acervo sem nenhum evento realizado responde **404**, nunca 200 com corpo nulo.

    200 com corpo nulo tornaria o tipo anulável em todo consumidor do contrato.
    """
    resposta = client.get("/api/v1/events/latest")

    assert resposta.status_code == 404
    assert "detail" in resposta.json()


def test_get_event_traz_summary_com_denominador(client: TestClient, db_session: Session) -> None:
    """O detalhe traz ``summary`` contando por método, com ``n_bouts`` como denominador.

    Os seis baldes (``dq`` incluído) somam ``n_bouts``: sem o balde de desqualificação as
    23 lutas por DQ do banco desapareceriam do resumo e a soma ficaria menor que o total
    sem erro nenhum.
    """
    event = _seed_event(db_session, "UFC 300", date(2024, 4, 13))
    _seed_card_realizado(db_session, event)

    body = client.get(f"/api/v1/events/{event.id}").json()

    resumo = body["summary"]
    assert resumo["n_bouts"] == 2
    assert resumo["ko_tko"] == 1
    assert resumo["decisions"] == 1
    assert set(resumo) == {
        "n_bouts",
        "ko_tko",
        "submissions",
        "decisions",
        "draws",
        "no_contests",
        "dq",
    }
    assert (
        resumo["ko_tko"]
        + resumo["submissions"]
        + resumo["decisions"]
        + resumo["draws"]
        + resumo["no_contests"]
        + resumo["dq"]
    ) == resumo["n_bouts"]


def test_get_event_traz_highlights_calculados(client: TestClient, db_session: Session) -> None:
    """O detalhe traz os quatro destaques; cada um nulo quando não há base (RF-13)."""
    event = _seed_event(db_session, "UFC 300", date(2024, 4, 13))
    ids = _seed_card_realizado(db_session, event)

    destaques = client.get(f"/api/v1/events/{event.id}").json()["highlights"]

    assert set(destaques) == {
        "fastest_finish",
        "most_sig_strikes",
        "most_takedowns",
        "biggest_upset",
    }
    assert destaques["fastest_finish"]["bout_id"] == ids["finalizacao"]
    assert destaques["fastest_finish"]["method"] == "ko_tko"
    assert destaques["fastest_finish"]["ending_time_seconds"] == 143
    assert destaques["fastest_finish"]["fighter"]["name"] == "Alex Pereira"
    assert destaques["most_sig_strikes"]["value"] == 42
    assert destaques["most_takedowns"]["value"] == 2
    # Sem nenhuma predição registrada não há zebra a declarar.
    assert destaques["biggest_upset"] is None


def test_get_event_so_agendado_traz_summary_e_highlights_nulos(
    client: TestClient, db_session: Session
) -> None:
    """Card inteiro agendado responde ``summary``/``highlights`` **nulos**, nunca zerados.

    Zero finalização é uma afirmação sobre o evento; ausência de resultado não é zero.
    """
    event = _seed_event(db_session, "UFC 335", date(2026, 12, 12))
    _seed_bout(db_session, event, status=BoutStatus.SCHEDULED, winner_id=None, method=None)

    body = client.get(f"/api/v1/events/{event.id}").json()

    assert body["summary"] is None
    assert body["highlights"] is None


def test_get_event_sem_lutas_traz_summary_e_highlights_nulos(
    client: TestClient, db_session: Session
) -> None:
    """Evento sem nenhuma luta (o ``UFC 335``, id 813) também declara ausência nos dois."""
    event = _seed_event(db_session, "UFC 335", date(2026, 12, 12))

    body = client.get(f"/api/v1/events/{event.id}").json()

    assert body["bouts"] == []
    assert body["summary"] is None
    assert body["highlights"] is None


def test_get_event_card_expoe_status_card_section_e_bout_order(
    client: TestClient, db_session: Session
) -> None:
    """``BoutCardOut`` expõe ``status``, ``card_section`` e ``bout_order``.

    ``card_section`` vem do **payload**, nunca derivado de ``bout_order`` (RF-27): a fórmula
    é da Cito (``1000 x seção + posição``) e pode mudar. ``bout_order`` não é sequencial nem
    denso -- o payload real traz **1001** na luta principal, que é o **menor** valor --,
    então só a ordem relativa é utilizável.
    """
    event = _seed_event(db_session, "UFC 300", date(2024, 4, 13))
    ids = _seed_card_realizado(db_session, event)

    cards = client.get(f"/api/v1/events/{event.id}").json()["bouts"]

    assert [card["id"] for card in cards] == [ids["finalizacao"], ids["decisao"]]
    assert cards[0]["status"] == "completed"
    assert cards[0]["card_section"] == "Main Card"
    assert cards[0]["bout_order"] == 1001
    assert cards[1]["card_section"] == "Prelims"
    assert cards[1]["bout_order"] == 2001


def test_get_event_card_expoe_status_scheduled_e_contexto_nulo(
    client: TestClient, db_session: Session
) -> None:
    """Luta agendada chega com ``status: scheduled`` e contexto de card nulo.

    O contexto da Cito só cobre evento recente e de forma irregular, então ``card_section``
    e ``bout_order`` nulos são o caso comum -- e a tela não pode depender deles.
    """
    event = _seed_event(db_session, "UFC 335", date(2026, 12, 12))
    _seed_bout(
        db_session,
        event,
        status=BoutStatus.SCHEDULED,
        winner_id=None,
        method=None,
        card_section=None,
        bout_order=None,
    )

    card = client.get(f"/api/v1/events/{event.id}").json()["bouts"][0]

    assert card["status"] == "scheduled"
    assert card["card_section"] is None
    assert card["bout_order"] is None
    assert card["method"] is None


def test_get_event_card_fighter_expoe_headshot_url(client: TestClient, db_session: Session) -> None:
    """``BoutCardFighterOut`` expõe ``headshot_url``, preenchido e nulo.

    O ``Fighter`` já vem eager-loaded no selector, então o campo custa zero query; sem ele a
    Home voltaria ao N+1 por lutador que o ADR 0003 eliminou.
    """
    event = _seed_event(db_session, "UFC 300", date(2024, 4, 13))
    _seed_card_realizado(db_session, event)

    card = client.get(f"/api/v1/events/{event.id}").json()["bouts"][0]

    por_canto = {f["corner"]: f for f in card["fighters"]}
    assert por_canto[Corner.RED.value]["headshot_url"] == "https://ufc.test/pereira.png"
    assert por_canto[Corner.BLUE.value]["headshot_url"] is None


def test_list_events_nao_traz_diagnostico_de_defasagem(
    client: TestClient, db_session: Session
) -> None:
    """Guarda de D-09: ``/events`` **não** carrega ``features_freshness``, nem no item.

    Nenhum endpoint de ``/events*`` lê o cache as-of, então nenhum deles tem defasagem a
    reportar. A SPEC 010 colocava o badge nesta slice e era erro: uma slice não exibe dado
    que seu endpoint não serve.
    """
    _seed_event(db_session, "UFC 300", date(2024, 4, 13))

    body = client.get("/api/v1/events").json()

    assert "features_freshness" not in body
    assert "features_freshness" not in body["items"][0]


def test_get_events_latest_nao_traz_diagnostico_de_defasagem(
    client: TestClient, db_session: Session
) -> None:
    """Guarda de D-09 estendida à rota nova: ``/events/latest`` também não o carrega."""
    event = _seed_event(db_session, "UFC 300", date(2024, 4, 13))
    _seed_card_realizado(db_session, event)

    body = client.get("/api/v1/events/latest").json()

    assert "features_freshness" not in body
