"""Testes do bloco D1 da SPEC 009 (Slice 05) -- base contra base.

Cobrem as três features do confronto de bases sobre DataFrame sintético (Pandas puro, sem
Postgres):

- as duas **bout-level** (``is_open_stance_matchup``, ``involves_switch_stance``), que
  cobrem as três categorias de confronto sem impor a ordem falsa que um encoding ordinal
  imporia, e que ficam **nulas** quando a base de qualquer canto é ausente ou desconhecida;
- a **por lutador** (``southpaw_opponents_faced_prior``), contagem point-in-time de
  adversários canhotos já enfrentados, com o teste anti-leakage dedicado (truncar as lutas
  futuras não muda o valor da luta N) e a regra do adversário sem base registrada.

A frame longa destes testes é a **enriquecida** (pós-trajetória): ``stance`` não é coluna
crua de ``long_frame.read_granular``, ela nasce em ``trajectory.add_physical_attributes``.
O dtype ``string`` (nullable) é reproduzido de propósito -- é ele que torna a conversão do
indicador binário para ``float64`` o ponto de falha real, e não uma sutileza de fixture.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from analysis.dataset import PROSCRIBED_FEATURE_BASES
from apps.bouts.enums import BoutMethod, Corner
from apps.fighters.enums import Stance
from ingestion.features.long_frame import LONG_FRAME_COLUMNS
from ingestion.features.matchup import (
    _BOUT_CONTEXT_BASES,
    _NON_FEATURE_BASES,
    _OUTCOME_BASES,
    _SUFFIX_DIFF,
    BOUT_LEVEL_FEATURE_COLUMNS,
    COL_GRAPPLING_AXIS,
    COL_STANCE,
    COL_VOLUME_AXIS,
    INVOLVES_SWITCH_STANCE,
    IS_OPEN_STANCE_MATCHUP,
    STANCE_MATCHUP_FEATURES,
    add_stance_matchup_features,
    build_matchup_matrix,
)
from ingestion.features.rolling import (
    RECENT_FORM_FEATURES,
    SOUTHPAW_OPPONENTS_FACED_PRIOR,
    STANCE_HISTORY_FEATURES,
    add_stance_history_features,
)
from ingestion.features.trajectory import STANCE

# Bases as-of e bout-level do bloco D1. Lista literal de propósito: se o módulo crescer sem
# que o bloco cresça junto, o registro da ablação continua falando do bloco certo.
_BLOCO_D1_BASES: tuple[str, ...] = (SOUTHPAW_OPPONENTS_FACED_PRIOR,)
_BLOCO_D1_BOUT_LEVEL: tuple[str, ...] = (IS_OPEN_STANCE_MATCHUP, INVOLVES_SWITCH_STANCE)

_ORTHODOX = Stance.ORTHODOX.value
_SOUTHPAW = Stance.SOUTHPAW.value
_SWITCH = Stance.SWITCH.value


def _matriz_de_bases(pares: list[tuple[str | None, str | None]]) -> pd.DataFrame:
    """Matriz de confronto sintética com apenas o par ``stance_a``/``stance_b``.

    Reproduz o dtype ``string`` (nullable) que ``trajectory.add_physical_attributes`` impõe
    à coluna: a base ausente chega como ``pd.NA``, não como ``None`` de objeto.
    """
    return pd.DataFrame(
        {
            "bout_id": list(range(1, len(pares) + 1)),
            f"{COL_STANCE}_a": pd.Series([a for a, _ in pares], dtype="string"),
            f"{COL_STANCE}_b": pd.Series([b for _, b in pares], dtype="string"),
        }
    )


@pytest.mark.parametrize(
    ("stance_a", "stance_b", "esperado_open", "esperado_switch"),
    [
        (_ORTHODOX, _ORTHODOX, 0.0, 0.0),
        (_ORTHODOX, _SOUTHPAW, 1.0, 0.0),
        (_SOUTHPAW, _ORTHODOX, 1.0, 0.0),
        (_SOUTHPAW, _SOUTHPAW, 0.0, 0.0),
        (_SWITCH, _ORTHODOX, 0.0, 1.0),
        (_ORTHODOX, _SWITCH, 0.0, 1.0),
        (_SWITCH, _SWITCH, 0.0, 1.0),
    ],
)
def test_confronto_de_bases_cobre_as_combinacoes_de_base_conhecida(
    stance_a: str, stance_b: str, esperado_open: float, esperado_switch: float
) -> None:
    """CA-01/CA-02 da SPEC 009: as duas binárias cobrem as três categorias de confronto.

    ``is_open_stance_matchup`` isola o clássico destro contra canhoto (nos dois sentidos --
    a feature é da luta, não do canto). ``involves_switch_stance`` marca a presença de um
    alternante em qualquer canto. Mesma base nos dois cantos zera as duas: é a terceira
    categoria, representada sem uma coluna própria e sem ordem falsa entre elas.
    """
    matriz = _matriz_de_bases([(stance_a, stance_b)])

    resultado = add_stance_matchup_features(matriz)

    assert resultado[IS_OPEN_STANCE_MATCHUP].iloc[0] == pytest.approx(esperado_open)
    assert resultado[INVOLVES_SWITCH_STANCE].iloc[0] == pytest.approx(esperado_switch)


@pytest.mark.parametrize(
    ("stance_a", "stance_b"),
    [
        (_ORTHODOX, None),
        (None, _SOUTHPAW),
        (None, None),
        (_ORTHODOX, "sideways"),
        ("open stance", _SWITCH),
    ],
)
def test_base_ausente_ou_desconhecida_anula_as_duas_binarias(
    stance_a: str | None, stance_b: str | None
) -> None:
    """CA-01/CA-02 (RF-03): base ausente ou fora do enum -> nulo nas duas, nunca zero.

    Zero afirmaria "não é confronto de bases opostas" sobre uma luta cuja base ninguém
    registrou -- imputação proibida. Rótulos como ``Open Stance`` e ``Sideways`` foram
    medidos no M7 e **zeram** a base do lutador no banco, mas a guarda cobre o caso de eles
    voltarem a atravessar a borda por outro caminho: base fora do enum é desconhecida, não
    uma quarta categoria.
    """
    matriz = _matriz_de_bases([(stance_a, stance_b)])

    resultado = add_stance_matchup_features(matriz)

    assert pd.isna(resultado[IS_OPEN_STANCE_MATCHUP].iloc[0])
    assert pd.isna(resultado[INVOLVES_SWITCH_STANCE].iloc[0])


def _corner_row(
    bout_id: int,
    corner: Corner,
    fighter_id: int,
    result: str,
    stance: str | None,
    event_day: int = 1,
) -> dict[str, object]:
    """Uma linha lutador-luta da frame longa **enriquecida** (pós-trajetória).

    Carrega o contexto de luta (contrato de ``LONG_FRAME_COLUMNS``) mais as colunas que
    nascem depois dele e que a matriz de confronto consome: ``stance`` (trajetória) e os
    dois eixos de estilo (forma recente). Valores constantes nos eixos -- o que este arquivo
    mede é o confronto de bases, não a distância de estilo.
    """
    return {
        "bout_id": bout_id,
        "corner": corner,
        "fighter_id": fighter_id,
        "result": result,
        "weight_class": "lightweight",
        "title_bout": False,
        "scheduled_rounds": 3,
        "event_date": date(2023, 1, event_day),
        "method": BoutMethod.DECISION.value,
        STANCE: stance,
        COL_GRAPPLING_AXIS: 0.5,
        COL_VOLUME_AXIS: 0.5,
    }


def test_features_de_confronto_de_bases_saem_como_coluna_unica() -> None:
    """CA-03 (CA-14 da SPEC): as duas binárias entram sem ``_a``/``_b`` e sem ``_diff``.

    São propriedade da **luta**, não do canto: virar par produziria um diferencial
    identicamente zero (ruído puro) e duplicaria a mesma informação em duas colunas.
    """
    long = pd.DataFrame(
        [
            _corner_row(1, Corner.RED, 10, "win", _ORTHODOX),
            _corner_row(1, Corner.BLUE, 20, "loss", _SOUTHPAW),
        ]
    )

    result = build_matchup_matrix(long)

    for coluna in _BLOCO_D1_BOUT_LEVEL:
        assert coluna in result.feature_columns, coluna
        for sufixo in ("_a", "_b", _SUFFIX_DIFF):
            assert f"{coluna}{sufixo}" not in result.frame.columns, f"{coluna}{sufixo}"
    assert result.frame[IS_OPEN_STANCE_MATCHUP].iloc[0] == pytest.approx(1.0)
    assert result.frame[INVOLVES_SWITCH_STANCE].iloc[0] == pytest.approx(0.0)


def _historico(bases_dos_adversarios: list[str | None]) -> pd.DataFrame:
    """Frame longa do lutador A com uma luta por adversário, na ordem cronológica.

    Uma linha por canto e por luta (o pareamento de adversário casa por ``bout_id``). A base
    de A é sempre ortodoxa; o que varia é a do adversário de cada luta, que é o insumo da
    contagem. Ordenada canonicamente, como a frame que chega de ``add_trajectory_features``.
    """
    linhas: list[dict[str, object]] = []
    for indice, base in enumerate(bases_dos_adversarios):
        bout_id = 100 + indice
        linhas.append(_corner_row(bout_id, Corner.RED, 1, "win", _ORTHODOX, event_day=indice + 1))
        linhas.append(
            _corner_row(bout_id, Corner.BLUE, 10 + indice, "loss", base, event_day=indice + 1)
        )
    frame = pd.DataFrame(linhas)
    frame[STANCE] = frame[STANCE].astype("string")
    return frame.sort_values(by=["fighter_id", "event_date", "bout_id"], kind="stable").reset_index(
        drop=True
    )


def _do_lutador_a(frame: pd.DataFrame) -> list[float]:
    """A contagem de canhotos do lutador A, em ordem cronológica."""
    linhas = frame.loc[frame["fighter_id"] == 1].sort_values("event_date", kind="stable")
    return [
        float(valor) if pd.notna(valor) else float("nan")
        for valor in linhas[SOUTHPAW_OPPONENTS_FACED_PRIOR]
    ]


def test_southpaw_opponents_faced_prior_conta_canhotos_anteriores() -> None:
    """CA-04 (RF-01): contagem point-in-time -- estreia nula, zero legítimo, acumulação.

    A enfrenta destro, canhoto, destro e canhoto, nessa ordem. Na estreia não há histórico
    (``NaN``); na 2a luta ele já enfrentou um destro e nenhum canhoto (``0`` -- valor
    legítimo, não ausência); na 3a, um canhoto; na 4a ainda um; e só na 5a chegaria a dois.
    A luta corrente **nunca** entra na própria contagem.
    """
    frame = _historico([_ORTHODOX, _SOUTHPAW, _ORTHODOX, _SOUTHPAW, _ORTHODOX])

    resultado = add_stance_history_features(frame)

    contagem = _do_lutador_a(resultado)
    assert pd.isna(contagem[0])
    assert contagem[1:] == [0.0, 1.0, 1.0, 2.0]


def test_southpaw_opponents_faced_prior_nao_muda_com_lutas_futuras() -> None:
    """CA-04 (RF-01): truncar as lutas posteriores não altera o valor da luta N.

    Teste anti-leakage dedicado, na mesma disciplina de ``test_rolling.py``: se a feature
    enxergasse o futuro, cortar a cauda da série mudaria os valores da cabeça. O corte é
    feito na frame **inteira** (os dois cantos das lutas futuras somem), que é a única forma
    de garantir que nem o pareamento de adversário reintroduz a informação.
    """
    completa = _historico([_ORTHODOX, _SOUTHPAW, _ORTHODOX, _SOUTHPAW, _SOUTHPAW])
    truncada = completa[completa["bout_id"] <= 102].reset_index(drop=True)

    de_completa = _do_lutador_a(add_stance_history_features(completa))
    de_truncada = _do_lutador_a(add_stance_history_features(truncada))

    assert de_truncada[1:] == de_completa[1 : len(de_truncada)]
    assert de_truncada[1:] == [0.0, 1.0]


def test_adversario_sem_base_nao_incrementa_nem_anula_a_contagem() -> None:
    """CA-04/CA-05: adversário sem base registrada é ignorado pela soma, não zerado.

    Decisão semântica fixada pelo plano (a SPEC é silenciosa sobre o contador): o indicador
    do adversário sem base é ``NaN`` e a soma acumulada o ignora -- a feature conta canhotos
    **entre os adversários de base conhecida**. Zerar afirmaria "era destro", que ninguém
    sabe; anular a contagem inteira destruiria a cobertura de quem tem histórico bom e um
    único adversário sem cadastro.

    A tem quatro lutas: canhoto, adversário sem base, destro, canhoto. A contagem da 3a luta
    continua ``1`` (a 2a não somou nem apagou nada) e a da 4a continua ``1``.
    """
    frame = _historico([_SOUTHPAW, None, _ORTHODOX, _SOUTHPAW])

    resultado = add_stance_history_features(frame)

    contagem = _do_lutador_a(resultado)
    assert pd.isna(contagem[0])
    assert contagem[1:] == [1.0, 1.0, 1.0]


def test_bloco_d1_nao_acrescenta_coluna_crua_a_frame_longa() -> None:
    """CA-09 (RF-02): a slice não projeta coluna crua nova, e ``stance`` não é desfecho.

    ``stance`` chega pela trajetória (M4), não por ``read_granular``: não está em
    ``LONG_FRAME_COLUMNS`` e não pode entrar em nenhuma das duas categorias de base
    excluída, sob pena de a classificação da RF-02 passar a falar de uma coluna que a frame
    crua não tem.
    """
    assert STANCE not in LONG_FRAME_COLUMNS
    assert STANCE not in _OUTCOME_BASES
    assert STANCE not in _BOUT_CONTEXT_BASES


def test_derivadas_do_bloco_d1_nao_entram_nas_bases_excluidas() -> None:
    """CA-09 (RF-02): as três derivadas do D1 são feature, não identidade/contexto/desfecho.

    O erro simétrico do anterior: classificar uma feature legítima como base excluída a
    tiraria do treino em silêncio -- o modo de falha que escondeu 21 features do M5. As duas
    bout-level entram pela allowlist (coluna única); a as-of vira o trio de canto.
    """
    for base in _BLOCO_D1_BASES:
        assert base not in _NON_FEATURE_BASES, base
        assert base not in BOUT_LEVEL_FEATURE_COLUMNS, base
        assert base in RECENT_FORM_FEATURES or base in STANCE_HISTORY_FEATURES, base
    for coluna in _BLOCO_D1_BOUT_LEVEL:
        assert coluna in BOUT_LEVEL_FEATURE_COLUMNS, coluna
        assert coluna in STANCE_MATCHUP_FEATURES, coluna


def test_nomes_do_bloco_d1_nao_colidem_com_as_bases_proscritas() -> None:
    """CA-09 (RF-10): nenhum nome do D1 é média de carreira proscrita do snapshot 2025.

    ``splm``, ``str_acc``, ``sapm``... embutem o resultado da luta que se quer prever
    (ADR 0002). A checagem cobre também as variantes de canto, porque é assim que a base
    as-of chega ao payload.
    """
    for nome in (*_BLOCO_D1_BASES, *_BLOCO_D1_BOUT_LEVEL):
        assert nome not in PROSCRIBED_FEATURE_BASES, nome
        for sufixo in ("_a", "_b", _SUFFIX_DIFF):
            assert f"{nome}{sufixo}" not in PROSCRIBED_FEATURE_BASES, f"{nome}{sufixo}"
