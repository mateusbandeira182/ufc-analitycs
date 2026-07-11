"""Testes do cliente HTTP da Cito API -- CA-03 e CA-04.

Cobrem o parsing do payload em um DTO tipado (``CitoEvent`` com metadados do evento
e a lista de lutas com os dois cantos) no modo fixture, sem tocar a rede nem gastar
quota; e o tratamento de erro/rate-limit no caminho HTTP real (via ``httpx.MockTransport``),
que converte 429 em ``CitoRateLimitError`` e demais erros em ``CitoError``, sem vazar
a exceção crua do ``httpx``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from apps.bouts.enums import Corner
from ingestion.cito.client import CitoClient, CitoError, CitoRateLimitError
from ingestion.cito.dto import CitoBoutStats, CitoEvent

_FIXTURES = Path(__file__).parent / "fixtures"
_EVENT_ID = "ufc-319"


def _fixture_client() -> CitoClient:
    return CitoClient(token="", base_url="https://mmaapi.dev", fixture_dir=_FIXTURES)


def test_fetch_event_no_modo_fixture_devolve_dto_tipado() -> None:
    """CA-03: o cliente em modo fixture parseia o payload num ``CitoEvent`` tipado."""
    event = _fixture_client().fetch_event(_EVENT_ID)

    assert isinstance(event, CitoEvent)
    assert event.event_id == _EVENT_ID
    assert event.name == "UFC 319: Du Plessis vs. Chimaev"
    assert event.date == date(2025, 8, 16)


def test_fetch_event_modela_lutas_com_os_dois_cantos() -> None:
    """CA-03: cada luta traz os dois cantos tipados (borda dinâmica sem ``Any``)."""
    event = _fixture_client().fetch_event(_EVENT_ID)

    assert len(event.bouts) == 2
    main = event.bouts[0]
    assert main.bout_id == "ufc-319-bout-1"
    slugs = {corner.slug for corner in main.corners}
    assert slugs == {"dricus-du-plessis", "khamzat-chimaev"}


def _mock_client(status_code: int) -> CitoClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "erro simulado"})

    transport = httpx.MockTransport(handler)
    return CitoClient(
        token="token-fake",
        base_url="https://mmaapi.dev",
        transport=transport,
    )


def test_rate_limit_vira_cito_rate_limit_error() -> None:
    """CA-04: resposta 429 vira ``CitoRateLimitError`` (subtipo de ``CitoError``)."""
    with pytest.raises(CitoRateLimitError):
        _mock_client(429).fetch_event(_EVENT_ID)


def test_erro_servidor_vira_cito_error() -> None:
    """CA-04: resposta 5xx vira ``CitoError``, sem vazar ``httpx.HTTPStatusError`` cru."""
    with pytest.raises(CitoError) as excinfo:
        _mock_client(503).fetch_event(_EVENT_ID)
    assert not isinstance(excinfo.value, CitoRateLimitError)


def test_fetch_event_via_http_parseia_payload_com_auth_e_params() -> None:
    """CA-03: ``fetch_event`` no caminho HTTP de sucesso parseia o payload em ``CitoEvent``.

    Simétrico ao sucesso do ``get_fighter`` via ``MockTransport``: exercita o parse HTTP real
    (não o modo fixture) e confirma o header ``Authorization: Bearer`` e o path/params corretos
    (``GET /api/v1/ufc/events?id=<event_id>``). Não toca a rede real nem consome quota.
    """
    payload = json.loads((_FIXTURES / f"event_{_EVENT_ID}.json").read_text(encoding="utf-8"))
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json=payload)

    client = CitoClient(
        token="token-fake",
        base_url="https://mmaapi.dev",
        transport=httpx.MockTransport(handler),
    )

    event = client.fetch_event(_EVENT_ID)

    assert isinstance(event, CitoEvent)
    assert event.event_id == _EVENT_ID
    assert event.name == "UFC 319: Du Plessis vs. Chimaev"
    assert len(event.bouts) == 2

    request = captured["request"]
    assert request.url.path == "/api/v1/ufc/events"
    assert request.url.params["id"] == _EVENT_ID
    assert request.headers["Authorization"] == "Bearer token-fake"


def test_fetch_bout_stats_via_http_parseia_payload_com_auth_e_path() -> None:
    """CA-01: ``fetch_bout_stats`` no caminho HTTP de sucesso parseia em ``CitoBoutStats``.

    Simétrico ao sucesso do ``get_fighter`` via ``MockTransport``: exercita o parse HTTP real
    das stats granulares por canto e confirma o header ``Authorization: Bearer`` e o path
    correto (``GET /api/v1/ufc/bouts/<bout_id>/stats``). Não toca a rede real nem consome quota.
    """
    bout_id = "ufc-319-bout-1"
    payload = json.loads((_FIXTURES / f"bout_stats_{bout_id}.json").read_text(encoding="utf-8"))
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json=payload)

    client = CitoClient(
        token="token-fake",
        base_url="https://mmaapi.dev",
        transport=httpx.MockTransport(handler),
    )

    stats = client.fetch_bout_stats(bout_id)

    assert isinstance(stats, CitoBoutStats)
    assert stats.bout_id == bout_id
    by_corner = {line.corner: line for line in stats.fighters}
    assert by_corner[Corner.RED].fighter_slug == "dricus-du-plessis"
    assert by_corner[Corner.BLUE].control_time_seconds == 1300

    request = captured["request"]
    assert request.url.path == f"/api/v1/ufc/bouts/{bout_id}/stats"
    assert request.headers["Authorization"] == "Bearer token-fake"
