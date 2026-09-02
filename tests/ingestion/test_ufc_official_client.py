"""Testes do cliente HTTP da API oficial da UFC -- CA-05 (e CA-02 no caminho do payload).

Cobrem o caminho HTTP de sucesso dos dois endpoints (a resposta 200 serve a **captura
verbatim**, e o teste afirma o path exato da requisição), o erro HTTP não-200, a falha de rede e
o payload que quebra o contrato. Nada aqui toca a rede: tudo passa por ``httpx.MockTransport``.

Não há modo fixture como no ``CitoClient``: aquele existe para não gastar a quota do free tier,
e a fonte oficial é gratuita e sem autenticação. Exercitar o caminho HTTP real com transporte
mockado é mais fiel do que um desvio de leitura de disco.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from apps.bouts.enums import Corner
from ingestion.ufc_official.client import UfcOfficialClient
from ingestion.ufc_official.dto import (
    UfcOfficialContractError,
    UfcOfficialError,
    UfcOfficialEvent,
    UfcOfficialFight,
)
from mma_analytics.settings import settings

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official"
_EVENT_ID = 1124
_FIGHT_ID = 10214


def _captura(filename: str) -> object:
    return json.loads((_FIXTURES / filename).read_text(encoding="utf-8"))


_Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: _Handler) -> UfcOfficialClient:
    return UfcOfficialClient(
        base_url="https://d29dxerjsp82wz.cloudfront.net",
        transport=httpx.MockTransport(handler),
    )


def test_fetch_event_usa_o_path_exato_e_devolve_dto() -> None:
    """CA-05: ``fetch_event`` bate em ``/api/v3/event/live/{id}.json`` e parseia a captura."""
    capturado: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado["request"] = request
        return httpx.Response(200, json=_captura(f"event_live_{_EVENT_ID}.json"))

    event = _client(handler).fetch_event(_EVENT_ID)

    assert isinstance(event, UfcOfficialEvent)
    assert event.event_id == _EVENT_ID
    assert len(event.fight_card) == 12
    assert capturado["request"].url.path == f"/api/v3/event/live/{_EVENT_ID}.json"


def test_fetch_fight_usa_o_path_exato_e_devolve_dto() -> None:
    """CA-05: ``fetch_fight`` bate em ``/api/v3/fight/live/{id}.json`` e parseia a captura."""
    capturado: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado["request"] = request
        return httpx.Response(200, json=_captura(f"fight_live_{_FIGHT_ID}.json"))

    fight = _client(handler).fetch_fight(_FIGHT_ID)

    assert isinstance(fight, UfcOfficialFight)
    assert fight.fight_id == _FIGHT_ID
    assert {fighter.corner for fighter in fight.fighters} == {Corner.RED, Corner.BLUE}
    assert capturado["request"].url.path == f"/api/v3/fight/live/{_FIGHT_ID}.json"


@pytest.mark.parametrize("status_code", [404, 500, 503])
def test_erro_http_vira_erro_tipado_sem_vazar_httpx(status_code: int) -> None:
    """CA-05: resposta não-200 vira ``UfcOfficialError``, nunca ``httpx.HTTPStatusError``.

    Um id inexistente (404) é esperado na varredura da Slice 02 -- mas quem decide pular é o
    chamador, com uma exceção do nosso módulo em mãos, não com uma do ``httpx``.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="erro simulado")

    with pytest.raises(UfcOfficialError) as excinfo:
        _client(handler).fetch_event(_EVENT_ID)

    assert not isinstance(excinfo.value, UfcOfficialContractError)
    assert str(_EVENT_ID) in str(excinfo.value)


def test_falha_de_rede_vira_erro_tipado() -> None:
    """CA-05: falha de rede vira ``UfcOfficialError``, sem vazar ``httpx.RequestError``."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexão recusada", request=request)

    with pytest.raises(UfcOfficialError) as excinfo:
        _client(handler).fetch_fight(_FIGHT_ID)

    assert not isinstance(excinfo.value, UfcOfficialContractError)


def test_payload_200_fora_do_contrato_vira_erro_de_contrato() -> None:
    """CA-02/CA-05: 200 com forma quebrada vira ``UfcOfficialContractError``, nunca ``None``.

    Um DTO meio preenchido é pior que uma exceção: ele segue adiante e grava dado incompleto.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"LiveEventDetail": {"EventId": _EVENT_ID}})

    with pytest.raises(UfcOfficialContractError):
        _client(handler).fetch_event(_EVENT_ID)


def test_resposta_200_que_nao_e_json_vira_erro_tipado() -> None:
    """CA-05: corpo não-JSON (página de erro do CDN) não vaza ``JSONDecodeError``."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>erro do CDN</html>")

    with pytest.raises(UfcOfficialError):
        _client(handler).fetch_event(_EVENT_ID)


def test_base_url_default_vem_das_settings() -> None:
    """CA-05: sem ``base_url`` explícito, o cliente usa a configuração da aplicação."""
    capturado: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado["request"] = request
        return httpx.Response(200, json=_captura(f"event_live_{_EVENT_ID}.json"))

    UfcOfficialClient(transport=httpx.MockTransport(handler)).fetch_event(_EVENT_ID)

    assert str(capturado["request"].url).startswith(settings.ufc_official_base_url)
