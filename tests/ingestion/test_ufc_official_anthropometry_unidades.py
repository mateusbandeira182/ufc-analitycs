"""Testes de conversão de unidade e de tolerância da antropometria oficial -- CA-01, CA-02, CA-05.

A fonte oficial publica altura e alcance em **polegadas** e peso em **libras**; o nosso domínio
guarda centímetros e quilos. Este é o risco de probabilidade **alta** da SPEC 008, com precedente
concreto: a Cito devolvia polegadas onde o DTO esperava centímetros e passou despercebido até a
SPEC 007.

Os valores esperados são calculados **à mão** e ancorados em medidas reais das capturas verbatim
(Darren Till: ``Height`` 72.0, ``Reach`` 74.5, ``Weight`` 185.0), nunca pela reimplementação da
fórmula dentro do teste -- um teste que repete a conta do código não prova conta nenhuma.

Nenhum teste deste arquivo toca banco ou rede: são funções puras.
"""

from __future__ import annotations

import pytest

from ingestion.ufc_official.anthropometry import (
    TOLERANCE_CM,
    TOLERANCE_KG,
    inches_to_cm,
    is_divergent_cm,
    is_divergent_kg,
    pounds_to_kg,
)

# --------------------------------------------------------------------------- #
# CA-01 -- polegadas viram centímetros
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("polegadas", "centimetros"),
    [
        # 71 x 2,54 = 180,34
        (71.0, 180),
        # 72 x 2,54 = 182,88 -- a altura real de Darren Till na captura verbatim
        (72.0, 183),
        # 73,5 x 2,54 = 186,69 -- meia polegada existe na fonte e arredondar na borda perderia
        # informação; a conversão é que arredonda, uma única vez
        (73.5, 187),
        # 74,5 x 2,54 = 189,23 -- o alcance real de Darren Till na captura verbatim
        (74.5, 189),
    ],
)
def test_inches_to_cm_converte_polegada_inteira_e_meia_polegada(
    polegadas: float, centimetros: int
) -> None:
    """CA-01: polegada (inteira ou meia) vira centímetro inteiro pelo fator 2,54."""
    assert inches_to_cm(polegadas) == centimetros


def test_inches_to_cm_ausencia_permanece_nula(caplog: pytest.LogCaptureFixture) -> None:
    """CA-05: alcance ausente na fonte permanece nulo -- nunca zero, nunca sentinela."""
    assert inches_to_cm(None) is None
    assert caplog.records == []


# --------------------------------------------------------------------------- #
# CA-01 -- libras viram quilos
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("libras", "quilos"),
    [
        # 185 x 0,45359237 = 83,914588... -> 83,91, que é exatamente o valor que o
        # ``fighter_details.csv`` do Kaggle já publica para o limite dos médios
        (185.0, 83.91),
        # 155 x 0,45359237 = 70,309... -> 70,31 (leves), idem no CSV
        (155.0, 70.31),
        # 145 x 0,45359237 = 65,770... -> 65,77 (penas), o peso do Volkanovski já persistido
        (145.0, 65.77),
    ],
)
def test_pounds_to_kg_converte_pelo_fator_da_libra_avoirdupois(
    libras: float, quilos: float
) -> None:
    """CA-01: libra vira quilo pelo fator exato 0,45359237, na convenção já persistida.

    O arredondamento a duas casas não é estético: é o que o Kaggle já gravou na base, e é o que
    faz o peso da fonte oficial **coincidir** com o nosso em vez de divergir por ruído de ponto
    flutuante na oitava casa.
    """
    assert pounds_to_kg(libras) == quilos


def test_pounds_to_kg_ausencia_permanece_nula() -> None:
    """CA-05: peso ausente na fonte permanece nulo -- nunca zero, nunca sentinela."""
    assert pounds_to_kg(None) is None


# --------------------------------------------------------------------------- #
# CA-02 -- tolerância na comparação com o valor persistido
# --------------------------------------------------------------------------- #


def test_diferenca_dentro_da_tolerancia_nao_e_divergencia() -> None:
    """CA-02: até 2 cm é arredondamento de polegada (369 de 371 casos medidos na SPEC)."""
    assert not is_divergent_cm(existing=180, from_source=182)
    assert not is_divergent_cm(existing=182, from_source=180)
    assert not is_divergent_cm(existing=180, from_source=180)


def test_diferenca_acima_da_tolerancia_e_divergencia() -> None:
    """CA-02: 3 cm já não cabe no arredondamento de uma polegada (2,54 cm) -- é divergência."""
    assert is_divergent_cm(existing=180, from_source=183)


def test_nulo_nunca_e_divergencia_em_centimetros() -> None:
    """CA-02: ausência de um dos lados não é discordância -- é só ausência."""
    assert not is_divergent_cm(existing=None, from_source=180)
    assert not is_divergent_cm(existing=180, from_source=None)
    assert not is_divergent_cm(existing=None, from_source=None)


def test_erro_de_unidade_aparece_como_divergencia_massiva() -> None:
    """CA-02: tratar centímetro como polegada estoura a tolerância -- o erro fica visível.

    É a razão de ser da tolerância apertada: 180 cm lidos como 180 polegadas viram 457 cm.
    Um limiar frouxo esconderia exatamente a classe de erro que a SPEC 007 pagou caro.
    """
    assert is_divergent_cm(existing=180, from_source=inches_to_cm(180.0))


def test_diferenca_de_peso_dentro_da_tolerancia_nao_e_divergencia() -> None:
    """CA-02: abaixo de meia libra convertida é arredondamento, não discordância."""
    assert not is_divergent_kg(existing=83.91, from_source=84.0)


def test_diferenca_de_divisao_de_peso_e_divergencia() -> None:
    """CA-02: 185 lb contra 155 lb é troca de divisão, não arredondamento."""
    assert is_divergent_kg(existing=83.91, from_source=70.31)


def test_nulo_nunca_e_divergencia_em_quilos() -> None:
    """CA-02: ausência de um dos lados não é discordância."""
    assert not is_divergent_kg(existing=None, from_source=83.91)
    assert not is_divergent_kg(existing=83.91, from_source=None)


def test_tolerancias_sao_as_medidas_na_spec() -> None:
    """CA-02: os limiares são constantes nomeadas, nunca números soltos no meio do código."""
    assert TOLERANCE_CM == 2
    assert TOLERANCE_KG == 0.5
