"""Testes do bloco D2 da SPEC 009 (Slice 05) -- eixos de estilo point-in-time.

Cobrem, sobre DataFrame sintético (Pandas puro, sem Postgres):

- os dois eixos por lutador (``grappling_axis_r3``, ``volume_axis_r3``), com a **aritmética
  exata** conferida contra os divisores nomeados (importados, nunca redigitados como número
  mágico), o truncamento em ``1.0``, o intervalo ``[0, 1]``, a propagação de nulo de
  qualquer componente e a corretude point-in-time linha a linha;
- a bout-level ``style_distance``, distância euclidiana entre os vetores de estilo as-of dos
  dois cantos, com a propagação de nulo e a garantia de coluna única;
- o **teste-guarda de drift** entre as constantes de nome duplicadas em ``matchup`` e as de
  origem em ``rolling``/``trajectory`` -- é ele que torna a duplicação deliberada segura.

A fórmula é normativa (SPEC 009, bloco D2) e os divisores são constantes de domínio: os
testes exercitam a fórmula, **nunca** justificam um ajuste dela.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from analysis.dataset import PROSCRIBED_FEATURE_BASES
from apps.bouts.enums import BoutMethod, Corner
from apps.fighters.enums import Stance
from ingestion.features import matchup as matchup_module
from ingestion.features import rolling as rolling_module
from ingestion.features.matchup import (
    _SUFFIX_DIFF,
    BOUT_LEVEL_FEATURE_COLUMNS,
    COL_GRAPPLING_AXIS,
    COL_STANCE,
    COL_VOLUME_AXIS,
    STYLE_DISTANCE,
    STYLE_MATCHUP_FEATURES,
    add_style_distance_feature,
    build_matchup_matrix,
)
from ingestion.features.rolling import (
    COL_FIGHTER_ID,
    GRAPPLING_AXIS_R3,
    GRAPPLING_CONTROL_SECONDS_DIVISOR,
    GRAPPLING_TAKEDOWNS_DIVISOR,
    RECENT_FORM_FEATURES,
    VOLUME_AXIS_R3,
    VOLUME_SIG_STRIKES_PM_DIVISOR,
    add_recent_form_features,
)
from ingestion.features.trajectory import STANCE

_FIGHTER_A = 1

# Bases as-of e bout-level do bloco D2 (lista literal: o registro da ablação continua
# falando do bloco certo mesmo se ``RECENT_FORM_FEATURES`` crescer por outro motivo).
_BLOCO_D2_BASES: tuple[str, ...] = (GRAPPLING_AXIS_R3, VOLUME_AXIS_R3)


def _participation(
    *,
    fighter_id: int,
    bout_id: int,
    event_day: int,
    sig_landed: float = 0,
    takedowns_landed: float = 0,
    control: float = 0.0,
    distance_landed: float = 0,
    clinch_landed: float = 0,
    ground_landed: float = 0,
) -> dict[str, object]:
    """Uma linha lutador-luta com as colunas cruas que os eixos de estilo consomem.

    Três rounds cheios (15 minutos de cativeiro) em toda luta, para que a taxa por minuto
    seja legível à mão. As colunas que os eixos não usam entram com ``0`` -- não é imputação,
    é fixture: o que este arquivo mede são os cinco componentes dos dois eixos.
    """
    return {
        COL_FIGHTER_ID: fighter_id,
        "bout_id": bout_id,
        "event_date": date(2024, 1, event_day),
        "result": "win",
        "method": BoutMethod.DECISION.value,
        "round": 3,
        "ending_time_seconds": 300,
        "sig_strikes_landed": sig_landed,
        "sig_strikes_attempted": 0,
        "takedowns_landed": takedowns_landed,
        "takedowns_attempted": 0,
        "control_time_seconds": control,
        "knockdowns": 0,
        "submission_attempts": 0,
        "total_strikes_landed": 0,
        "head_landed": 0,
        "body_landed": 0,
        "leg_landed": 0,
        "distance_landed": distance_landed,
        "clinch_landed": clinch_landed,
        "ground_landed": ground_landed,
        "head_attempted": 0,
        "body_attempted": 0,
        "leg_attempted": 0,
        "distance_attempted": 0,
        "clinch_attempted": 0,
        "ground_attempted": 0,
        "scheduled_rounds": 3,
    }


def _frame(lutas: list[dict[str, float]]) -> pd.DataFrame:
    """Frame longa do lutador A, uma luta por entrada, com o canto oposto preenchido.

    O adversário existe em cada luta porque o pareamento por ``bout_id`` é parte da pipeline
    real (``_opponent_stats``); as stats dele são irrelevantes para os eixos e ficam zeradas.
    Ordenada canonicamente por ``(fighter_id, event_date, bout_id)``.
    """
    linhas: list[dict[str, object]] = []
    for indice, luta in enumerate(lutas):
        bout_id = 200 + indice
        linhas.append(
            _participation(fighter_id=_FIGHTER_A, bout_id=bout_id, event_day=indice + 1, **luta)
        )
        linhas.append(
            _participation(fighter_id=100 + indice, bout_id=bout_id, event_day=indice + 1)
        )
    return (
        pd.DataFrame(linhas)
        .sort_values(by=[COL_FIGHTER_ID, "event_date", "bout_id"], kind="stable")
        .reset_index(drop=True)
    )


def _eixos_do_lutador_a(frame: pd.DataFrame) -> pd.DataFrame:
    """As linhas do lutador A, em ordem cronológica, reindexadas de 0."""
    linhas = frame.loc[frame[COL_FIGHTER_ID] == _FIGHTER_A]
    return linhas.sort_values("event_date", kind="stable").reset_index(drop=True)


def test_eixos_de_estilo_seguem_a_formula_normativa_com_os_divisores_nomeados() -> None:
    """CA-06 da SPEC 009: os eixos são a média explícita dos componentes normalizados.

    A luta 1 de A tem 1 queda, 150 s de controle e 10 golpes conectados por posição (6 na
    distância, 1 no clinch, 3 no solo), com 30 golpes significativos em 15 minutos. Na luta 2
    esses são os únicos dados anteriores, então:

    - ``grappling_axis_r3`` = média(1/3,0 truncado, 150/300,0 truncado, share_ground=0,3);
    - ``volume_axis_r3``    = média(2,0/6,0 truncado, share_distance=0,6).

    O valor esperado é montado **a partir das constantes importadas**, não de números
    mágicos: um divisor alterado tem de quebrar o teste por divergência de fórmula, jamais
    passar porque o número foi redigitado dos dois lados.
    """
    frame = _frame(
        [
            {
                "takedowns_landed": 1,
                "control": 150.0,
                "sig_landed": 30,
                "distance_landed": 6,
                "clinch_landed": 1,
                "ground_landed": 3,
            },
            {},
        ]
    )

    resultado = _eixos_do_lutador_a(add_recent_form_features(frame))

    grappling = (
        1.0 / GRAPPLING_TAKEDOWNS_DIVISOR + 150.0 / GRAPPLING_CONTROL_SECONDS_DIVISOR + 0.3
    ) / 3.0
    volume = (2.0 / VOLUME_SIG_STRIKES_PM_DIVISOR + 0.6) / 2.0
    assert resultado[GRAPPLING_AXIS_R3].iloc[1] == pytest.approx(grappling)
    assert resultado[VOLUME_AXIS_R3].iloc[1] == pytest.approx(volume)


def test_componentes_acima_do_divisor_truncam_em_um_e_os_eixos_ficam_em_zero_um() -> None:
    """CA-06: ``min(x/divisor, 1)`` -- o dominante satura, não estoura o intervalo.

    A luta 1 é de um grappler extremo: 5 quedas, 600 s de controle e 100% do golpe conectado
    no solo, com 150 golpes significativos em 15 minutos (10 por minuto). Os três componentes
    do eixo de grappling saturam, e o eixo vale exatamente ``1,0`` -- nunca mais que isso. O
    de volume mistura um componente saturado com ``share_distance`` zero e dá ``0,5``, o que
    mostra que os dois eixos descrevem dimensões diferentes do mesmo lutador.
    """
    frame = _frame(
        [
            {
                "takedowns_landed": 5,
                "control": 600.0,
                "sig_landed": 150,
                "ground_landed": 10,
            },
            {},
        ]
    )

    resultado = _eixos_do_lutador_a(add_recent_form_features(frame))

    assert resultado[GRAPPLING_AXIS_R3].iloc[1] == pytest.approx(1.0)
    assert resultado[VOLUME_AXIS_R3].iloc[1] == pytest.approx(0.5)
    for eixo in _BLOCO_D2_BASES:
        definidos = resultado[eixo].dropna()
        assert ((definidos >= 0.0) & (definidos <= 1.0)).all(), eixo


def test_componente_nulo_anula_o_eixo_sem_media_parcial() -> None:
    """CA-07 (RF-03): um componente ausente torna o eixo NULO -- jamais média dos demais.

    O modo de falha mais provável do bloco, e o mais silencioso: ``DataFrame.mean(axis=1)``
    ignora ``NaN`` e devolveria a média dos componentes que existem. O eixo sairia definido,
    mas calculado sobre um subconjunto diferente de componentes por lutador -- e comparar
    eixos entre lutadores é exatamente o que ``style_distance`` e o bloco D3 fazem.

    Aqui o tempo de controle da única luta anterior é ausente (backfill parcial, dado real do
    banco): ``grappling_axis_r3`` fica nulo mesmo com os outros dois componentes definidos,
    enquanto ``volume_axis_r3``, que não depende dele, continua computável.
    """
    frame = _frame(
        [
            {
                "takedowns_landed": 1,
                "control": float("nan"),
                "sig_landed": 30,
                "distance_landed": 6,
                "ground_landed": 4,
            },
            {},
        ]
    )

    resultado = _eixos_do_lutador_a(add_recent_form_features(frame))

    assert pd.isna(resultado[GRAPPLING_AXIS_R3].iloc[1])
    assert resultado[VOLUME_AXIS_R3].iloc[1] == pytest.approx(
        (2.0 / VOLUME_SIG_STRIKES_PM_DIVISOR + 0.6) / 2.0
    )


def test_eixos_sao_point_in_time_e_nulos_na_estreia() -> None:
    """CA-06 (RF-01): a estreia é nula e truncar as lutas futuras não muda a luta N.

    Os eixos herdam a corretude temporal por construção -- os cinco componentes já passaram
    por ``shift(1)`` e aqui não há agregação nova --, o que **não** dispensa a prova: é a
    herança que se está afirmando, e ela some no dia em que alguém acrescentar um sexto
    componente calculado de outro jeito.
    """
    completa = _frame(
        [
            {"takedowns_landed": 1, "control": 150.0, "sig_landed": 30, "ground_landed": 4},
            {"takedowns_landed": 3, "control": 300.0, "sig_landed": 90, "ground_landed": 9},
            {"takedowns_landed": 0, "control": 0.0, "sig_landed": 10, "distance_landed": 10},
        ]
    )
    truncada = completa[completa["bout_id"] <= 201].reset_index(drop=True)

    de_completa = _eixos_do_lutador_a(add_recent_form_features(completa))
    de_truncada = _eixos_do_lutador_a(add_recent_form_features(truncada))

    for eixo in _BLOCO_D2_BASES:
        assert pd.isna(de_completa[eixo].iloc[0]), eixo
        assert de_truncada[eixo].iloc[1] == pytest.approx(de_completa[eixo].iloc[1]), eixo


def _matriz_de_estilo(
    pares: list[tuple[tuple[float, float] | None, tuple[float, float] | None]],
) -> pd.DataFrame:
    """Matriz de confronto sintética com só os pares de eixo dos dois cantos.

    ``None`` representa o vetor de estilo indisponível de um canto (estreante, ou lutador
    cujo histórico não tem os componentes) -- o caso que a RF-03 manda propagar como nulo.
    """

    def _eixo(vetor: tuple[float, float] | None, indice: int) -> float:
        return float("nan") if vetor is None else vetor[indice]

    return pd.DataFrame(
        {
            "bout_id": list(range(1, len(pares) + 1)),
            f"{COL_GRAPPLING_AXIS}_a": [_eixo(a, 0) for a, _ in pares],
            f"{COL_VOLUME_AXIS}_a": [_eixo(a, 1) for a, _ in pares],
            f"{COL_GRAPPLING_AXIS}_b": [_eixo(b, 0) for _, b in pares],
            f"{COL_VOLUME_AXIS}_b": [_eixo(b, 1) for _, b in pares],
        }
    )


def test_style_distance_e_a_distancia_euclidiana_entre_os_dois_vetores() -> None:
    """CA-08: distância euclidiana no quadrado unitário -- zero se idênticos, √2 no extremo.

    O par ``(1, 0)`` contra ``(0, 1)`` é o confronto mais dissemelhante possível (grappler
    puro contra volumoso puro) e dá exatamente ``√2`` -- o teto do intervalo que sustenta a
    escolha de σ no bloco D3. Estilos idênticos dão ``0``.
    """
    matriz = _matriz_de_estilo(
        [((1.0, 0.0), (0.0, 1.0)), ((0.4, 0.7), (0.4, 0.7)), ((0.5, 0.2), (0.1, 0.5))]
    )

    resultado = add_style_distance_feature(matriz)

    assert resultado[STYLE_DISTANCE].iloc[0] == pytest.approx(2.0**0.5)
    assert resultado[STYLE_DISTANCE].iloc[1] == pytest.approx(0.0)
    assert resultado[STYLE_DISTANCE].iloc[2] == pytest.approx((0.4**2 + 0.3**2) ** 0.5)


def test_style_distance_e_nula_quando_qualquer_eixo_de_qualquer_canto_falta() -> None:
    """CA-08 (RF-03): vetor de estilo incompleto -> distância nula, nunca parcial.

    Uma distância calculada sobre um eixo só não é comparável com uma calculada sobre dois:
    seria a mesma coluna medindo duas grandezas diferentes, em silêncio.
    """
    matriz = _matriz_de_estilo([(None, (0.3, 0.4)), ((0.3, 0.4), None), (None, None)])
    matriz.loc[3] = {
        "bout_id": 4,
        f"{COL_GRAPPLING_AXIS}_a": 0.3,
        f"{COL_VOLUME_AXIS}_a": float("nan"),
        f"{COL_GRAPPLING_AXIS}_b": 0.5,
        f"{COL_VOLUME_AXIS}_b": 0.6,
    }

    resultado = add_style_distance_feature(matriz)

    assert resultado[STYLE_DISTANCE].isna().all()


def _corner_row(
    bout_id: int,
    corner: Corner,
    fighter_id: int,
    result: str,
    grappling: float,
    volume: float,
) -> dict[str, object]:
    """Uma linha lutador-luta da frame longa **enriquecida** (pós-rolling e trajetória)."""
    return {
        "bout_id": bout_id,
        "corner": corner,
        "fighter_id": fighter_id,
        "result": result,
        "weight_class": "lightweight",
        "title_bout": False,
        "scheduled_rounds": 3,
        "event_date": date(2023, 1, 1),
        "method": BoutMethod.DECISION.value,
        STANCE: Stance.ORTHODOX.value,
        COL_GRAPPLING_AXIS: grappling,
        COL_VOLUME_AXIS: volume,
    }


def test_style_distance_sai_como_coluna_unica_no_orquestrador() -> None:
    """CA-03 (CA-14 da SPEC): ``style_distance`` entra sem ``_a``/``_b`` e sem ``_diff``.

    É uma **relação** entre os dois cantos, não um atributo de nenhum deles: um par seria
    duas cópias do mesmo número e o diferencial, zero em toda linha.
    """
    long = pd.DataFrame(
        [
            _corner_row(1, Corner.RED, 10, "win", 1.0, 0.0),
            _corner_row(1, Corner.BLUE, 20, "loss", 0.0, 1.0),
        ]
    )

    result = build_matchup_matrix(long)

    assert STYLE_DISTANCE in result.feature_columns
    for sufixo in ("_a", "_b", _SUFFIX_DIFF):
        assert f"{STYLE_DISTANCE}{sufixo}" not in result.frame.columns, sufixo
    assert result.frame[STYLE_DISTANCE].iloc[0] == pytest.approx(2.0**0.5)
    # Os dois eixos, esses sim, são do lutador: viram o trio de canto.
    for base in _BLOCO_D2_BASES:
        for sufixo in ("_a", "_b", _SUFFIX_DIFF):
            assert f"{base}{sufixo}" in result.feature_columns, f"{base}{sufixo}"


def test_constantes_de_nome_duplicadas_em_matchup_coincidem_com_as_de_origem() -> None:
    """Guarda de drift: a duplicação deliberada de nome de coluna só é segura com este teste.

    ``matchup`` declara os nomes de ``stance`` e dos dois eixos localmente, em vez de
    importá-los, para não acoplar os módulos de feature (mesma convenção de
    ``trajectory.COL_ROUND_NUMBER``). O preço é este teste: sem ele, renomear a coluna na
    origem faria as features bout-level lerem uma coluna inexistente -- ou, pior, sumirem em
    silêncio -- sem que nada acusasse.
    """
    assert matchup_module.COL_GRAPPLING_AXIS == rolling_module.GRAPPLING_AXIS_R3
    assert matchup_module.COL_VOLUME_AXIS == rolling_module.VOLUME_AXIS_R3
    assert matchup_module.COL_STANCE == STANCE
    assert rolling_module.COL_STANCE == STANCE
    assert COL_STANCE == STANCE


def test_eixos_de_estilo_entram_no_conjunto_de_features_de_forma_recente() -> None:
    """Os dois eixos são emitidos como coluna as-of por lutador, não como intermediárias.

    Diferente das gêmeas de carreira do bloco C (Séries locais): estes eixos **são** o
    entregável do D2 e o insumo declarado do D3 (Slice 06), então precisam sobreviver até o
    payload -- e ``RECENT_FORM_FEATURES`` é o que o CLI conta ao reportar o estágio.
    """
    for base in _BLOCO_D2_BASES:
        assert base in RECENT_FORM_FEATURES, base
    assert STYLE_DISTANCE in BOUT_LEVEL_FEATURE_COLUMNS
    assert STYLE_MATCHUP_FEATURES == [STYLE_DISTANCE]


def test_nomes_do_bloco_d2_nao_colidem_com_as_bases_proscritas() -> None:
    """CA-09 (RF-10): nenhum nome do D2 é média de carreira proscrita do snapshot 2025."""
    for nome in (*_BLOCO_D2_BASES, STYLE_DISTANCE):
        assert nome not in PROSCRIBED_FEATURE_BASES, nome
        for sufixo in ("_a", "_b", _SUFFIX_DIFF):
            assert f"{nome}{sufixo}" not in PROSCRIBED_FEATURE_BASES, f"{nome}{sufixo}"
