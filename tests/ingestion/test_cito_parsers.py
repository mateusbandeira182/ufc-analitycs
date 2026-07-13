"""Testes dos parsers de string da Cito -- CA-01, CA-02.

Cobrem a conversão das strings de estatística (``"66 of 125"`` -> ``(66, 125)``) e de tempo
(``"7:13"`` -> ``433`` segundos) na borda dinâmica do payload da Cito. A distinção
inegociável: **ausência** (``None``/``""``) degrada de forma explícita (``(None, None)`` /
``None`` -- não se inventa zero), enquanto **formato inesperado** levanta ``CitoParseError``
(erro tipado, nunca silencioso nem ``Any``).
"""

from __future__ import annotations

import pytest

from ingestion.cito.parsers import CitoParseError, parse_clock, parse_stat


def test_parse_stat_converte_landed_de_attempted() -> None:
    """CA-01: ``"66 of 125"`` vira a tupla ``(66, 125)`` (landed, attempted)."""
    assert parse_stat("66 of 125") == (66, 125)


def test_parse_stat_aceita_zero_de_zero() -> None:
    """CA-01: ``"0 of 0"`` (nenhuma tentativa) vira ``(0, 0)`` -- valor legítimo, não ausência."""
    assert parse_stat("0 of 0") == (0, 0)


def test_parse_stat_tolera_espacos_ao_redor() -> None:
    """CA-01: espaços em volta da string não quebram o parse."""
    assert parse_stat("  41 of 120  ") == (41, 120)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parse_stat_ausencia_degrada_para_none(raw: str | None) -> None:
    """CA-01: ausência (``None``/``""``/só espaços) degrada para ``(None, None)`` -- sem zero."""
    assert parse_stat(raw) == (None, None)


@pytest.mark.parametrize("raw", ["abc", "66/125", "66 of", "of 125", "66 of 125 of 3"])
def test_parse_stat_formato_inesperado_levanta_erro_tipado(raw: str) -> None:
    """CA-01: formato diferente de ``"N of M"`` levanta ``CitoParseError`` (nunca silencioso)."""
    with pytest.raises(CitoParseError):
        parse_stat(raw)


def test_parse_clock_converte_minutos_e_segundos_para_segundos() -> None:
    """CA-02: ``"7:13"`` vira ``433`` segundos (7*60 + 13)."""
    assert parse_clock("7:13") == 433


def test_parse_clock_aceita_zero_zero() -> None:
    """CA-02: ``"0:00"`` vira ``0`` -- tempo de controle nulo é valor legítimo."""
    assert parse_clock("0:00") == 0


@pytest.mark.parametrize("raw", [None, "", "--"])
def test_parse_clock_ausencia_degrada_para_none(raw: str | None) -> None:
    """CA-02: ausência (``None``/``""``/``"--"``) degrada para ``None``, sem inventar zero."""
    assert parse_clock(raw) is None


@pytest.mark.parametrize("raw", ["7:60", "abc", "7", "7:13:00", "-1:00", "7:-5"])
def test_parse_clock_formato_inesperado_levanta_erro_tipado(raw: str) -> None:
    """CA-02: formato inválido (segundos >= 60, sem ``:``, negativo) levanta ``CitoParseError``."""
    with pytest.raises(CitoParseError):
        parse_clock(raw)


def test_cito_parse_error_e_subclasse_de_value_error() -> None:
    """CA-01/CA-02: ``CitoParseError`` é ``ValueError`` (standalone, evita ciclo de import)."""
    assert issubclass(CitoParseError, ValueError)
