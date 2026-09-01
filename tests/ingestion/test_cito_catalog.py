"""Testes do catálogo de eventos da Cito -- DTO do item e cliente paginado (Slice 03).

Cobrem CA-02 e CA-03 da sprint 007-03:

- **DTO** (``CitoCatalogItem``/``CitoCatalogMeta``/``CitoCatalogEnvelope``) validado contra
  ``events_catalog_page_1.json`` -- a resposta **real e integral** de
  ``GET https://api.citoapi.com/api/v1/ufc/events``, capturada em 2026-08-31 (50 itens,
  ``meta.total=811``, ``meta.totalPages=17``). É o contrato: se a Cito mudar o payload, o
  teste quebra aqui, na borda, e não no meio da ingestão.
- **Cliente paginado** (``fetch_event_catalog``) exercitado sobre o conjunto **derivado** em
  ``fixtures/catalog_paginado_derivado/`` -- duas páginas montadas com itens **reais** da
  captura, com o ``meta`` ajustado para encerrar na página 2 (a página real sozinha é 1 de 17
  e não permite percorrer o catálogo até o fim offline).

Nenhum teste toca a rede real: modo fixture (JSON local) ou ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from ingestion.cito.client import (
    CallBudget,
    CitoClient,
    CitoError,
    CitoRateLimitError,
    QuotaExceededError,
)
from ingestion.cito.dto import CitoCatalogEnvelope, CitoCatalogItem

_FIXTURES = Path(__file__).parent / "fixtures"
# Conjunto derivado (2 páginas, itens reais recortados) -- ver docstring do módulo.
_CATALOGO_PAGINADO = _FIXTURES / "catalog_paginado_derivado"


def _payload_real() -> object:
    """Carrega o payload real e integral da página 1 do catálogo (captura de 2026-08-31)."""
    return json.loads((_FIXTURES / "events_catalog_page_1.json").read_text(encoding="utf-8"))


def _cliente_paginado(budget: CallBudget | None = None) -> CitoClient:
    """Cliente em modo fixture sobre o conjunto derivado de duas páginas."""
    return CitoClient(
        token="",
        base_url="https://api.citoapi.com",
        fixture_dir=_CATALOGO_PAGINADO,
        budget=budget,
    )


# --------------------------------------------------------------------------- #
# CA-03 -- DTO do item de catálogo contra o payload real
# --------------------------------------------------------------------------- #


def test_envelope_valida_o_payload_real_do_catalogo() -> None:
    """CA-03: o envelope ``{success, data, meta}`` real valida sem perder item nem meta."""
    envelope = CitoCatalogEnvelope.model_validate(_payload_real())

    assert envelope.success is True
    assert len(envelope.data) == 50
    assert envelope.meta.page == 1
    assert envelope.meta.limit == 50
    assert envelope.meta.total == 811
    assert envelope.meta.total_pages == 17
    assert envelope.meta.has_next_page is True
    assert envelope.meta.next_page == 2


def test_item_resolve_alias_camel_case_e_ignora_extras() -> None:
    """CA-03: alias camelCase resolvido; campos extras do payload real não estouram."""
    envelope = CitoCatalogEnvelope.model_validate(_payload_real())
    item = next(i for i in envelope.data if i.slug == "ufc-335")

    assert item.id == "db421fc1-05eb-47de-be41-21f2c5c313ec"
    assert item.title == "UFC 335"
    assert item.short_title == "UFC 335"
    assert item.status == "scheduled"
    assert item.venue == "T-Mobile Arena"
    assert item.city == "Las Vegas"
    assert item.location_text == "T-Mobile Arena, Las Vegas United States"
    # ``imageUrl``/``dataAvailability``/``broadcastInfo`` etc. são descartados por extra="ignore".
    assert not hasattr(item, "image_url")


def test_starts_at_e_timezone_aware() -> None:
    """CA-03: ``starts_at`` é um instante timezone-aware (nunca naive)."""
    envelope = CitoCatalogEnvelope.model_validate(_payload_real())
    item = next(i for i in envelope.data if i.slug == "ufc-335")

    assert item.starts_at == datetime(2026, 12, 13, 2, 0, tzinfo=UTC)
    assert item.starts_at.tzinfo is not None


def test_local_date_usa_o_event_date_do_catalogo() -> None:
    """CA-03/CA-04: ``local_date`` é a data **local** do evento, não a data UTC de ``starts_at``.

    ``ufc-335`` começa em 2026-12-13 UTC e tem ``eventDate`` 2026-12-12 -- é essa a data que o
    slug e o seed usam. Comparar a data UTC crua deslocaria o casamento em um dia.
    """
    envelope = CitoCatalogEnvelope.model_validate(_payload_real())
    item = next(i for i in envelope.data if i.slug == "ufc-335")

    assert item.starts_at.date() == date(2026, 12, 13)
    assert item.event_date == date(2026, 12, 12)
    assert item.local_date == date(2026, 12, 12)


def test_local_date_degrada_para_a_data_utc_sem_event_date() -> None:
    """CA-03: sem ``eventDate`` no payload, ``local_date`` cai para a data UTC de ``starts_at``.

    Ausência explícita: o item continua casável (pela janela de tolerância), sem inventar data.
    """
    item = CitoCatalogItem.model_validate(
        {
            "id": "sem-event-date",
            "slug": "ufc-999",
            "title": "UFC 999",
            "status": "scheduled",
            "startsAt": "2026-12-13T02:00:00.000Z",
        }
    )

    assert item.event_date is None
    assert item.local_date == date(2026, 12, 13)


def test_fight_night_real_tem_data_local_um_dia_antes_da_utc() -> None:
    """CA-04: a armadilha central, com dado real -- o slug usa a data local, não a UTC."""
    envelope = CitoCatalogEnvelope.model_validate(_payload_real())
    item = next(i for i in envelope.data if i.slug == "ufc-fight-night-august-22-2026")

    assert item.starts_at.date() == date(2026, 8, 23)
    assert item.local_date == date(2026, 8, 22)


# --------------------------------------------------------------------------- #
# CA-02 -- cliente paginado
# --------------------------------------------------------------------------- #


def test_fetch_event_catalog_percorre_todas_as_paginas() -> None:
    """CA-02: a paginação junta os itens das duas páginas e para em ``hasNextPage`` falso."""
    itens = _cliente_paginado().fetch_event_catalog()

    assert len(itens) == 9
    slugs = [item.slug for item in itens]
    assert slugs[0] == "ufc-335"
    assert "ufc-fight-night-august-22-2026" in slugs  # veio da página 2
    assert "ufc-328" in slugs


def test_fetch_event_catalog_cobra_uma_unidade_de_quota_por_pagina() -> None:
    """CA-02: cada página custa uma unidade do ``CallBudget`` (o custo modela o free tier)."""
    budget = CallBudget(limit=10)

    _cliente_paginado(budget).fetch_event_catalog()

    assert budget.used == 2


def test_fetch_event_catalog_com_quota_insuficiente_interrompe_sem_parcial() -> None:
    """CA-02: teto estourado no meio da paginação levanta; nenhum resultado parcial é devolvido."""
    budget = CallBudget(limit=1)

    with pytest.raises(QuotaExceededError):
        _cliente_paginado(budget).fetch_event_catalog()

    assert budget.used == 1


def test_fetch_event_catalog_para_quando_next_page_nao_avanca() -> None:
    """CA-02: ``nextPage`` que não avança encerra a paginação -- nunca laço infinito na API.

    Guarda contra um ``meta`` inconsistente da API (``hasNextPage`` verdadeiro apontando para
    uma página já visitada): a execução para em vez de queimar quota indefinidamente.
    """
    paginas: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paginas.append(int(request.url.params["page"]))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [],
                "meta": {
                    "page": 1,
                    "limit": 50,
                    "total": 0,
                    "totalPages": 1,
                    "hasNextPage": True,
                    "nextPage": 1,
                },
            },
        )

    client = CitoClient(
        token="token-fake",
        base_url="https://api.citoapi.com",
        transport=httpx.MockTransport(handler),
    )

    assert client.fetch_event_catalog() == []
    assert paginas == [1]


def test_fetch_event_catalog_envia_page_limit_from_e_to_na_query() -> None:
    """CA-02: o caminho HTTP envia ``page``/``limit``/``from``/``to`` como query params."""
    capturadas: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        capturadas.append(request.url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [],
                "meta": {
                    "page": 1,
                    "limit": 25,
                    "total": 0,
                    "totalPages": 1,
                    "hasNextPage": False,
                    "nextPage": None,
                },
            },
        )

    client = CitoClient(
        token="token-fake",
        base_url="https://api.citoapi.com",
        transport=httpx.MockTransport(handler),
    )
    client.fetch_event_catalog(limit=25, date_from=date(2019, 1, 1), date_to=date(2025, 12, 31))

    assert len(capturadas) == 1
    url = capturadas[0]
    assert url.path == "/api/v1/ufc/events"
    assert url.params["page"] == "1"
    assert url.params["limit"] == "25"
    assert url.params["from"] == "2019-01-01"
    assert url.params["to"] == "2025-12-31"


def test_fetch_event_catalog_rate_limit_vira_erro_tipado() -> None:
    """CA-02: 429 no catálogo vira ``CitoRateLimitError``, sem vazar exceção crua do ``httpx``."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "erro simulado"})

    client = CitoClient(
        token="token-fake",
        base_url="https://api.citoapi.com",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CitoRateLimitError):
        client.fetch_event_catalog()


def test_fetch_event_catalog_success_false_vira_cito_error() -> None:
    """CA-02: envelope com ``success=false`` vira ``CitoError`` -- nunca silencioso."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": False,
                "data": [],
                "meta": {
                    "page": 1,
                    "limit": 50,
                    "total": 0,
                    "totalPages": 1,
                    "hasNextPage": False,
                    "nextPage": None,
                },
            },
        )

    client = CitoClient(
        token="token-fake",
        base_url="https://api.citoapi.com",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CitoError):
        client.fetch_event_catalog()


def test_paginacao_ignora_o_limit_clampado_pela_api() -> None:
    """CA-02: a Cito **clampa** ``limit`` em 100 e a paginação não pode depender do valor pedido.

    Comportamento real observado numa sondagem autorizada (2026-08-31): pedir ``limit=200``
    devolve ``meta.limit=100`` sem erro nem aviso. A paginação segue ``hasNextPage``/``nextPage``,
    então o clamp não a quebra -- este teste blinda essa independência.
    """
    paginas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paginas.append(request.url.params["limit"])
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [],
                "meta": {
                    "page": page,
                    "limit": 100,  # clampado pela API, diferente do solicitado
                    "total": 200,
                    "totalPages": 2,
                    "hasNextPage": page < 2,
                    "nextPage": page + 1 if page < 2 else None,
                },
            },
        )

    client = CitoClient(
        token="token-fake",
        base_url="https://api.citoapi.com",
        transport=httpx.MockTransport(handler),
    )
    client.fetch_event_catalog(limit=200)

    assert paginas == ["200", "200"]  # o pedido segue como enviado; o clamp é da API
