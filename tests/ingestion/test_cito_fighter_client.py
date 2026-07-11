"""Testes do fetch de perfil de lutador da Cito (``GET /fighters/{slug}``) -- CA-07.

Cobrem o parsing do payload de perfil num DTO tipado (``CitoFighter`` com a data de
nascimento, base do desempate da resolução cross-source) no modo fixture, sem tocar a
rede nem gastar quota; e o tratamento de erro/rate-limit no caminho HTTP real (via
``httpx.MockTransport``), que converte 429 em ``CitoRateLimitError`` e demais erros em
``CitoError``, sem vazar a exceção crua do ``httpx``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from apps.fighters.enums import Stance
from ingestion.cito.client import CitoClient, CitoError, CitoRateLimitError
from ingestion.cito.dto import CitoFighter

_FIXTURES = Path(__file__).parent / "fixtures"
_SLUG = "alexander-volkanovski"


def _fixture_client() -> CitoClient:
    return CitoClient(token="", base_url="https://mmaapi.dev", fixture_dir=_FIXTURES)


def test_get_fighter_no_modo_fixture_devolve_dto_com_dob() -> None:
    """CA-07: o cliente em modo fixture parseia o perfil num ``CitoFighter`` com DOB."""
    fighter = _fixture_client().get_fighter(_SLUG)

    assert isinstance(fighter, CitoFighter)
    assert fighter.slug == _SLUG
    assert fighter.name == "Alexander Volkanovski"
    assert fighter.date_of_birth == date(1988, 9, 29)
    assert fighter.nickname == "The Great"
    assert fighter.stance is Stance.ORTHODOX
    assert (fighter.wins, fighter.losses, fighter.draws) == (27, 4, 0)


def _mock_client(status_code: int) -> CitoClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "erro simulado"})

    transport = httpx.MockTransport(handler)
    return CitoClient(token="token-fake", base_url="https://mmaapi.dev", transport=transport)


def test_get_fighter_rate_limit_vira_cito_rate_limit_error() -> None:
    """CA-07: resposta 429 no fetch de perfil vira ``CitoRateLimitError``."""
    with pytest.raises(CitoRateLimitError):
        _mock_client(429).get_fighter(_SLUG)


def test_get_fighter_erro_servidor_vira_cito_error() -> None:
    """CA-07: resposta 5xx no fetch de perfil vira ``CitoError``, sem vazar o erro do httpx."""
    with pytest.raises(CitoError) as excinfo:
        _mock_client(503).get_fighter(_SLUG)
    assert not isinstance(excinfo.value, CitoRateLimitError)
