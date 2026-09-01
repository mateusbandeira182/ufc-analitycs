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
    return CitoClient(token="", base_url="https://api.citoapi.com", fixture_dir=_FIXTURES)


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
    return CitoClient(token="token-fake", base_url="https://api.citoapi.com", transport=transport)


def test_get_fighter_rate_limit_vira_cito_rate_limit_error() -> None:
    """CA-07: resposta 429 no fetch de perfil vira ``CitoRateLimitError``."""
    with pytest.raises(CitoRateLimitError):
        _mock_client(429).get_fighter(_SLUG)


def test_get_fighter_erro_servidor_vira_cito_error() -> None:
    """CA-07: resposta 5xx no fetch de perfil vira ``CitoError``, sem vazar o erro do httpx."""
    with pytest.raises(CitoError) as excinfo:
        _mock_client(503).get_fighter(_SLUG)
    assert not isinstance(excinfo.value, CitoRateLimitError)


# --------------------------------------------------------------------------- #
# Contrato REAL do endpoint de perfil (sondagem autorizada de 2026-09-01, Slice 06).
#
# O payload do M1 era uma suposição escrita antes de a API real ser medida -- mesma classe
# de erro que a ADR 0005 corrigiu para o endpoint de stats. O real embrulha em
# ``{success, data, meta}``, usa camelCase e publica antropometria em POLEGADAS.
# --------------------------------------------------------------------------- #

_SLUG_REAL = "bruno-silva"


def test_get_fighter_desembrulha_o_envelope_do_payload_real() -> None:
    """O envelope ``{success, data, meta}`` é desembrulhado e mapeado no ``CitoFighter``.

    Fixture do payload **real** capturado em 2026-09-01. Validar o envelope como se fosse o
    perfil cru falhava com ``ValidationError`` -- foi o que interrompeu o lote do gap.
    """
    fighter = _fixture_client().get_fighter(_SLUG_REAL)

    assert isinstance(fighter, CitoFighter)
    assert fighter.slug == _SLUG_REAL
    assert fighter.name == "Bruno Silva"
    assert fighter.nickname == "Bulldog"
    assert (fighter.wins, fighter.losses, fighter.draws) == (15, 9, 2)
    assert fighter.stance is Stance.ORTHODOX


def test_get_fighter_converte_antropometria_de_polegadas_para_centimetros() -> None:
    """``heightInches``/``reachInches`` (string, polegadas) viram centímetros inteiros.

    O schema guarda centímetros desde o M0; converter na borda evita que a unidade da fonte
    vaze para o domínio. 64 pol -> 163 cm; 65 pol -> 165 cm.
    """
    fighter = _fixture_client().get_fighter(_SLUG_REAL)

    assert fighter.height_cm == 163
    assert fighter.reach_cm == 165


def test_get_fighter_mantem_data_de_nascimento_ausente_como_none() -> None:
    """``birthDate`` nulo permanece ``None`` -- nunca derivada de ``age``.

    Achado da sondagem de 2026-09-01: o perfil real de 'bruno-silva' traz ``birthDate: null``
    e ``age: 36``. Calcular a data a partir da idade produziria uma DOB **inventada** com
    precisão de um ano -- e ela entraria justamente na chave de desempate da entity
    resolution, que existe para separar homônimos. Ausência é reportada como ausência.
    """
    assert _fixture_client().get_fighter(_SLUG_REAL).date_of_birth is None
