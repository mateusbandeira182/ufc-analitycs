"""Testes da normalização determinística de nome (chave de dedup) -- CA-01.

Fixam a intenção: variações de caixa, acento, espaços múltiplos e sufixos do
mesmo nome colapsam para a mesma chave; nomes distintos produzem chaves distintas.
"""

from __future__ import annotations

import pytest

from ingestion.normalize import normalize_name

_VOLKANOVSKI = "alexander volkanovski"


@pytest.mark.parametrize(
    "raw",
    [
        "Alexander Volkanovski",
        "alexander volkanovski",
        "ALEXANDER VOLKANOVSKI",
        "alexander  volkanovski",
        "  Alexander   Volkanovski  ",
        "Alexander Vólkanovski",
        "Alexander Volkanovski Jr",
        "Alexander Volkanovski Jr.",
    ],
)
def test_variacoes_do_mesmo_nome_colapsam_para_a_mesma_chave(raw: str) -> None:
    """Caixa, acento, espaço múltiplo e sufixo do mesmo nome dão a mesma chave."""
    assert normalize_name(raw) == _VOLKANOVSKI


def test_nomes_distintos_produzem_chaves_distintas() -> None:
    """Lutadores diferentes não colidem na chave normalizada."""
    assert normalize_name("Bruno Silva") != normalize_name("Alexander Volkanovski")


def test_sufixos_romanos_sao_removidos() -> None:
    """Sufixos de linhagem (II, III, IV, Sr) não fazem parte da chave."""
    assert normalize_name("Marlon Vera III") == normalize_name("Marlon Vera")
    assert normalize_name("Dan Severn Sr") == normalize_name("Dan Severn")


def test_normalizacao_e_deterministica() -> None:
    """Chamar duas vezes com a mesma entrada devolve o mesmo resultado."""
    assert normalize_name("José Aldo") == normalize_name("José Aldo")
    assert normalize_name("José Aldo") == "jose aldo"
