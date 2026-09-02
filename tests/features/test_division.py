"""Testes das features de divisão -- Plano 009-02, bloco B1 (CA-08 e CA-10).

Pandas puro, sem Postgres: ``ingestion.features.division`` é conhecimento de domínio sobre
o rótulo de ``bouts.weight_class`` e sobre a taxa base histórica da divisão, e nada nele
depende do banco. Cobrem:

- **B1a (categoria)** -- o mapa explícito das 12 divisões para o limite de peso real em
  libras, o indicador de divisão feminina (as femininas colidem em peso com as masculinas
  em 125/135/145) e a normalização do rótulo cru (caixa, espaços, apóstrofo tipográfico,
  sufixo de disputa de cinturão). Rótulo desconhecido produz **nulo nas duas colunas**, com
  ``logger.warning`` nomeando o rótulo uma vez por rótulo distinto -- nunca zero, nunca
  chute (RF-03).
- **B1b (taxa base point-in-time)** -- a fração de finalizações da divisão em lutas de data
  **estritamente anterior**. O teste de não-vazamento é o ponto de risco declarado do bloco:
  duas lutas da mesma divisão no mesmo evento não podem se enxergar (``shift(1)`` posicional
  deixaria), e uma luta de data posterior enxerga as duas.
"""

from __future__ import annotations

import logging
import math
from datetime import date

import pandas as pd
import pytest

from apps.bouts.enums import BoutMethod
from ingestion.features.division import (
    DIVISION_BASE_RATE_FEATURES,
    DIVISION_CATEGORY_FEATURES,
    DIVISION_FINISH_RATE_PRIOR,
    IS_WOMENS_DIVISION,
    WEIGHT_CLASS_LBS,
    WEIGHT_CLASS_LIMIT_LBS,
    division_category_features,
    division_finish_rate_prior,
    normalize_division_keys,
)

# As 12 divisões da SPEC, com o limite de peso real e o indicador de divisão feminina.
_DIVISOES: tuple[tuple[str, int, float], ...] = (
    ("flyweight", 125, 0.0),
    ("bantamweight", 135, 0.0),
    ("featherweight", 145, 0.0),
    ("lightweight", 155, 0.0),
    ("welterweight", 170, 0.0),
    ("middleweight", 185, 0.0),
    ("light heavyweight", 205, 0.0),
    ("heavyweight", 265, 0.0),
    ("women's strawweight", 115, 1.0),
    ("women's flyweight", 125, 1.0),
    ("women's bantamweight", 135, 1.0),
    ("women's featherweight", 145, 1.0),
)


def _categoria(rotulos: list[object]) -> pd.DataFrame:
    """Atalho: rótulos crus -> chaves canônicas -> as duas colunas de categoria."""
    return division_category_features(normalize_division_keys(pd.Series(rotulos, dtype="object")))


def test_mapa_das_doze_divisoes_traduz_para_libras_e_genero() -> None:
    """CA-08 (B1a): cada uma das 12 divisões vira o limite real em libras e o gênero.

    O eixo é o **limite de peso de verdade**, não um índice arbitrário: a árvore pode
    cortá-lo e o corte significa algo no mundo real. E as três colisões de peso
    (125/135/145 aparecem no masculino e no feminino) só se distinguem por
    ``is_womens_division`` -- sem ele, o modelo veria peso-mosca feminino e masculino como
    a mesma coisa, com taxas base muito diferentes.
    """
    rotulos = [nome for nome, _, _ in _DIVISOES]

    features = _categoria(list(rotulos))

    assert list(features.columns) == DIVISION_CATEGORY_FEATURES
    for posicao, (nome, libras, feminina) in enumerate(_DIVISOES):
        assert features[WEIGHT_CLASS_LBS].iloc[posicao] == pytest.approx(float(libras)), nome
        assert features[IS_WOMENS_DIVISION].iloc[posicao] == pytest.approx(feminina), nome
    # As colisões de peso existem de fato -- é o que torna o indicador necessário.
    assert WEIGHT_CLASS_LIMIT_LBS["flyweight"] == WEIGHT_CLASS_LIMIT_LBS["women's flyweight"]


def test_normalizacao_absorve_caixa_espaco_e_apostrofo_tipografico() -> None:
    """Variações de grafia do mesmo rótulo resolvem para a mesma chave canônica.

    O banco real tem ``Lightweight`` e ``lightweight`` convivendo (o seed do Kaggle e a Cito
    grafam diferente), e o apóstrofo tipográfico aparece em rótulo feminino copiado de
    página web. Sem a normalização, a mesma divisão viraria três divisões distintas e
    fragmentaria a taxa base do B1b.
    """
    features = _categoria(
        ["Lightweight", "  LIGHTWEIGHT  ", "Light   Heavyweight", "Women’s Bantamweight"]
    )

    assert list(features[WEIGHT_CLASS_LBS]) == [155.0, 155.0, 205.0, 135.0]
    assert list(features[IS_WOMENS_DIVISION]) == [0.0, 0.0, 0.0, 1.0]


def test_sufixo_de_disputa_de_cinturao_resolve_para_a_mesma_divisao() -> None:
    """``"Middleweight Title"`` é a MESMA divisão -- o cinturão é do bloco B2, não do B1.

    Rótulo real de ``bouts.weight_class`` (vem do ``weightClass`` da Cito; ver
    ``tests/ingestion/test_incremental.py``). Tratá-lo como desconhecido jogaria fora
    justamente as lutas de título -- as mais importantes do card -- e criaria uma divisão
    fantasma na taxa base.
    """
    features = _categoria(["Middleweight Title", "Women's Strawweight Title"])

    assert list(features[WEIGHT_CLASS_LBS]) == [185.0, 115.0]
    assert list(features[IS_WOMENS_DIVISION]) == [0.0, 1.0]


@pytest.mark.parametrize("rotulo", ["Catch Weight", "open weight", "", None])
def test_rotulo_desconhecido_produz_nulo_nas_duas_colunas(rotulo: object) -> None:
    """CA-08 (RF-03): fora do mapa -> nulo nas duas colunas, nunca zero, nunca chute.

    Zero em ``weight_class_lbs`` seria um peso impossível e zero em ``is_womens_division``
    afirmaria "masculino" sobre uma luta cuja divisão ninguém conhece.
    """
    features = _categoria([rotulo])

    assert math.isnan(features[WEIGHT_CLASS_LBS].iloc[0])
    assert math.isnan(features[IS_WOMENS_DIVISION].iloc[0])


def test_rotulo_desconhecido_avisa_uma_vez_por_rotulo_com_a_contagem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CA-08: o aviso nomeia o rótulo e sai **uma vez por rótulo distinto**, não por linha.

    O banco real tem 126 rótulos distintos, dezenas deles torneios do TUF com uma luta cada.
    Avisar por linha afogaria o log e esconderia o que a RF-09 quer que apareça: quais
    rótulos ficaram de fora e quantas lutas cada um custa.
    """
    rotulos = ["catch weight", "catch weight", "catch weight", "open weight", "lightweight"]

    with caplog.at_level(logging.WARNING, logger="ingestion.features.division"):
        normalize_division_keys(pd.Series(rotulos, dtype="object"))

    avisos = [record.getMessage() for record in caplog.records]
    assert len(avisos) == 2  # um por rótulo distinto desconhecido
    assert any("catch weight" in aviso and "3" in aviso for aviso in avisos)
    assert any("open weight" in aviso for aviso in avisos)
    assert not any("lightweight" in aviso for aviso in avisos)


def test_normalizacao_de_serie_toda_nula_nao_avisa(caplog: pytest.LogCaptureFixture) -> None:
    """Ausência de rótulo não é rótulo desconhecido -- nada a nomear, nada a avisar."""
    with caplog.at_level(logging.WARNING, logger="ingestion.features.division"):
        chaves = normalize_division_keys(pd.Series([None, None], dtype="object"))

    assert chaves.isna().all()
    assert caplog.records == []


def _lutas(
    linhas: list[tuple[str | None, date, BoutMethod]],
) -> pd.Series:
    """Atalho: (rótulo, data, método) por luta -> ``division_finish_rate_prior``."""
    frame = pd.DataFrame(
        {
            "weight_class": [rotulo for rotulo, _, _ in linhas],
            "event_date": [data for _, data, _ in linhas],
            "method": [metodo.value for _, _, metodo in linhas],
        }
    )
    return division_finish_rate_prior(
        normalize_division_keys(frame["weight_class"]),
        frame["event_date"],
        frame["method"],
    )


def test_taxa_base_nao_vaza_entre_lutas_do_mesmo_evento() -> None:
    """CA-10: o ponto de risco do bloco -- duas lutas do mesmo evento não se enxergam.

    ``shift(1)`` posicional sobre a série ordenada deixaria a segunda luta do card enxergar
    o desfecho da primeira, que **não é conhecido** no momento em que se prevê o card
    inteiro. O corte tem de ser por ``event_date`` estritamente menor, e não "linha
    anterior". A luta de data posterior enxerga as duas: 1 finalização em 2 lutas -> 0,5.
    """
    mesmo_evento = date(2023, 1, 1)
    taxas = _lutas(
        [
            ("lightweight", mesmo_evento, BoutMethod.KO_TKO),
            ("lightweight", mesmo_evento, BoutMethod.DECISION),
            ("lightweight", date(2023, 6, 1), BoutMethod.DECISION),
        ]
    )

    # Nenhuma das duas do mesmo evento tem data anterior a olhar -> nulo (nunca 0).
    assert math.isnan(taxas.iloc[0])
    assert math.isnan(taxas.iloc[1])
    # A posterior enxerga exatamente as duas anteriores: 1 finalização de 2 lutas.
    assert taxas.iloc[2] == pytest.approx(0.5)


def test_taxa_base_de_lutas_do_mesmo_evento_e_identica() -> None:
    """CA-10: lutas do mesmo evento compartilham o mesmo passado -- valor idêntico.

    Corolário do corte por data: se duas lutas da mesma divisão caem no mesmo evento, o
    conjunto de lutas anteriores é o mesmo para as duas. Um valor diferente entre elas
    denunciaria corte posicional.
    """
    taxas = _lutas(
        [
            ("welterweight", date(2022, 1, 1), BoutMethod.SUBMISSION),
            ("welterweight", date(2023, 1, 1), BoutMethod.KO_TKO),
            ("welterweight", date(2023, 1, 1), BoutMethod.DECISION),
        ]
    )

    assert taxas.iloc[1] == pytest.approx(1.0)
    assert taxas.iloc[2] == pytest.approx(1.0)


def test_taxa_base_da_primeira_data_de_uma_divisao_e_nula() -> None:
    """CA-08 (RF-03): sem nenhuma luta anterior o denominador é zero -> nulo, nunca 0."""
    taxas = _lutas([("heavyweight", date(2020, 1, 1), BoutMethod.KO_TKO)])

    assert math.isnan(taxas.iloc[0])


def test_taxa_base_nao_mistura_divisoes_distintas() -> None:
    """A taxa é propriedade da divisão: o histórico de uma não entra no denominador da outra."""
    taxas = _lutas(
        [
            ("heavyweight", date(2022, 1, 1), BoutMethod.KO_TKO),
            ("heavyweight", date(2022, 2, 1), BoutMethod.KO_TKO),
            ("women's strawweight", date(2022, 3, 1), BoutMethod.DECISION),
            ("heavyweight", date(2022, 4, 1), BoutMethod.DECISION),
        ]
    )

    # A luta feminina é a primeira da divisão dela -> nula, apesar das duas anteriores no card.
    assert math.isnan(taxas.iloc[2])
    # A quarta luta só enxerga as duas de pesados: 2 finalizações em 2 lutas.
    assert taxas.iloc[3] == pytest.approx(1.0)


def test_taxa_base_de_rotulo_desconhecido_e_nula() -> None:
    """Rótulo fora do mapa não forma divisão fantasma: fica nulo, sem denominador próprio."""
    taxas = _lutas(
        [
            ("catch weight", date(2022, 1, 1), BoutMethod.KO_TKO),
            ("catch weight", date(2022, 6, 1), BoutMethod.KO_TKO),
        ]
    )

    assert taxas.isna().all()


def test_taxa_base_conta_nc_e_empate_no_denominador_como_nao_finalizacao() -> None:
    """Convenção herdada de ``rolling.FINISH_RATE_PRIOR``: só KO/TKO e finalização contam.

    NC e empate são lutas que aconteceram naquela divisão e não terminaram em finalização;
    tirá-las do denominador inflaria a taxa base artificialmente.
    """
    taxas = _lutas(
        [
            ("flyweight", date(2022, 1, 1), BoutMethod.KO_TKO),
            ("flyweight", date(2022, 2, 1), BoutMethod.NO_CONTEST),
            ("flyweight", date(2022, 3, 1), BoutMethod.DECISION),
            ("flyweight", date(2022, 4, 1), BoutMethod.DECISION),
        ]
    )

    assert taxas.iloc[3] == pytest.approx(1 / 3)


def test_taxa_base_preserva_a_ordem_das_linhas_de_entrada() -> None:
    """A série devolvida está alinhada à entrada, ainda que ela chegue fora de ordem.

    A matriz de confronto não é ordenada por data (o pivô a devolve na ordem do merge), e um
    desalinhamento silencioso atribuiria a taxa base de uma luta a outra.
    """
    taxas = _lutas(
        [
            ("lightweight", date(2023, 6, 1), BoutMethod.DECISION),
            ("lightweight", date(2022, 1, 1), BoutMethod.KO_TKO),
        ]
    )

    assert taxas.iloc[0] == pytest.approx(1.0)  # a de 2023 enxerga a de 2022
    assert math.isnan(taxas.iloc[1])  # a de 2022 é a primeira da divisão


def test_listas_nomeadas_dos_dois_sub_blocos_sao_disjuntas() -> None:
    """Os dois sub-blocos são medidos como vereditos independentes -- colunas sem interseção."""
    assert set(DIVISION_CATEGORY_FEATURES).isdisjoint(DIVISION_BASE_RATE_FEATURES)
    assert DIVISION_BASE_RATE_FEATURES == [DIVISION_FINISH_RATE_PRIOR]
