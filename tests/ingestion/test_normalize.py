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


# --- Transliteração de letra atômica (M9, SPEC 011, RF-09 / CA-10) -----------------------
#
# O NFKD **não** decompõe letra atômica (ela não é 'letra + diacrítico'), então o
# ``encode("ascii", "ignore")`` a APAGAVA em vez de transliterá-la. O efeito é silencioso e
# duplo: 'Michał Oleksiejczuk' virava 'micha oleksiejczuk', que **não** casa com o
# 'michal oleksiejczuk' já persistido do seed -- a entity resolution criava um lutador novo e
# a contagem de estreantes inflava, sem erro nenhum.
#
# Os dois nomes abaixo são reais e estão na base: 'Michal Oleksiejczuk' (semeado do Kaggle,
# grafia ASCII) e 'Jan Błachowicz' (criado pela Cito, grafia com ``ł``) -- é exatamente o par
# que o defeito produz.

_LETRAS_ATOMICAS = [
    ("ł", "l"),
    ("ø", "o"),
    ("đ", "d"),
    ("ß", "ss"),
    ("æ", "ae"),
    ("œ", "oe"),
    ("þ", "th"),
    ("ð", "d"),
]


@pytest.mark.parametrize(("letra", "esperado"), _LETRAS_ATOMICAS)
def test_letra_atomica_e_transliterada_nao_apagada(letra: str, esperado: str) -> None:
    """As oito letras atômicas da RF-09 viram o equivalente ASCII, nunca o vazio."""
    assert normalize_name(f"a{letra}b") == f"a{esperado}b"


@pytest.mark.parametrize(("letra", "esperado"), _LETRAS_ATOMICAS)
def test_letra_atomica_em_caixa_alta_tambem_e_transliterada(letra: str, esperado: str) -> None:
    """A tabela opera depois do ``casefold``, então a versão maiúscula translitera igual."""
    assert normalize_name(f"A{letra.upper()}B") == f"a{esperado}b"


def test_nome_real_com_l_cortado_casa_com_a_grafia_ascii() -> None:
    """'Michał Oleksiejczuk' e 'Michal Oleksiejczuk' colapsam para a mesma chave (CA-10)."""
    assert normalize_name("Michał Oleksiejczuk") == "michal oleksiejczuk"
    assert normalize_name("Michał Oleksiejczuk") == normalize_name("Michal Oleksiejczuk")


def test_nome_real_persistido_pela_cito_casa_com_a_grafia_do_seed() -> None:
    """'Jan Błachowicz' (Cito) e 'Jan Blachowicz' (Kaggle) são a mesma pessoa, mesma chave."""
    assert normalize_name("Jan Błachowicz") == "jan blachowicz"
    assert normalize_name("Jan Błachowicz") == normalize_name("Jan Blachowicz")


def test_acento_decomponivel_continua_sendo_removido() -> None:
    """A tabela nova não desliga o NFKD: 'José Aldo' segue virando 'jose aldo'."""
    assert normalize_name("José Aldo") == "jose aldo"
    assert normalize_name("Šarić") == "saric"
