"""Paridade entre o cache as-of e a pipeline ao vivo (RF-03 / CA-03 da SPEC 011).

Arquivo próprio, e não mais um caso em ``tests/features/test_asof.py``, porque esta é a
**amarra de primeira classe** da slice: é literalmente a cicatriz do ``bout_features``
(21 features descartadas em silêncio por meses) sendo prevenida no grão lutador. Se o vetor
materializado divergir do que a pipeline produz, o modelo passa a comer um vetor diferente do
que viu no treino -- e nada acusa.

Dois níveis, ambos com ``as_of`` fixado na **mesma** data dos dois lados:

- **Nível 1 (por lutador)**: base a base, o ``features`` cacheado bate com a linha sintética
  que ``analysis.predict._asof_matchup_rows`` produz ao vivo (``1e-9``, ou ambos nulos).
- **Nível 2 (por par, o vetor servido)**: a matriz montada do cache, **alinhada a
  ``feature_names``**, bate coluna a coluna com a montada pela pipeline ao vivo -- inclusive o
  D3 derivado do ``style_history`` e todos os ``*_diff``. É este nível que amarra o que o
  modelo de fato come.

Mais a **invariante que torna o cache por lutador correto**: as features as-of de A não mudam
com a identidade de B, exceto ``similar_style_win_rate_prior`` (feature do PAR, D-15). Sem
teste, ela é suposição.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from analysis.model import load_artifact
from analysis.predict import _align_features, _asof_matchup_rows, _cached_matchup_rows
from apps.features.selectors import read_asof_features
from ingestion.features.asof import (
    asof_payload_columns,
    build_asof_materialization,
    enrich_asof,
    materialize_asof_features,
)
from ingestion.features.long_frame import build_long_frame, read_granular
from ingestion.features.matchup import COL_BOUT_ID
from ingestion.features.rolling import (
    SIMILAR_STYLE_WIN_RATE_PRIOR,
    similar_style_prior_for_opponent,
)
from tests.analysis.test_predict import _seed_history, _train_and_persist

# Tolerância normativa da RF-03.
_TOLERANCIA = 1e-9

# ``as_of`` dos dois lados da comparação. É a data que a produção usa (``datetime.now``), e
# fixá-la numa variável é o que garante que cache e pipeline ao vivo estejam medindo o mesmo
# instante -- uma diferença de um dia moveria ``age_years``/``layoff_days`` e a paridade
# falharia por motivo errado.
_HOJE = datetime.now(UTC).date()

_SUFIXOS = ("_a", "_b")


def _materializar(session: Session) -> None:
    """Materializa o cache as-of na data de hoje, sem commitar (transação do teste)."""
    materialize_asof_features(session, build_asof_materialization(session, _HOJE))


def _bases_as_of(session: Session) -> list[str]:
    """Conjunto de bases do payload, vindo da fonte única -- nunca redigitado.

    Percorrer uma lista escrita à mão tornaria a paridade vacuamente verdadeira para toda
    feature que alguém esquecesse de acrescentar: o teste juraria paridade sobre um
    subconjunto.
    """
    return asof_payload_columns(enrich_asof(session, build_long_frame(read_granular(session))))


def _iguais(esperado: object, obtido: object) -> bool:
    """Igualdade de valor de feature: ``1e-9`` de tolerância, ou **ambos** ausentes."""
    esperado_nulo = esperado is None or (
        isinstance(esperado, float) and math.isnan(float(esperado))
    )
    obtido_nulo = obtido is None or (isinstance(obtido, float) and math.isnan(float(obtido)))
    if esperado_nulo or obtido_nulo:
        return esperado_nulo and obtido_nulo
    return math.isclose(float(esperado), float(obtido), abs_tol=_TOLERANCIA)  # type: ignore[arg-type]


def test_paridade_nivel_1_o_vetor_cacheado_bate_base_a_base(db_session: Session) -> None:
    """CA-03: o ``features`` de cada lutador é o que a pipeline ao vivo produz (``1e-9``).

    Os dois lados são caminhos de código independentes: o cache injeta uma sintética **por
    lutador** (sem adversário) e o serving ao vivo injeta uma **por par**. Que os dois cheguem
    ao mesmo número é a prova de que a cadeia as-of é a mesma -- e de que a identidade do
    adversário não vaza para o vetor por lutador.
    """
    ids = _seed_history(db_session)
    _materializar(db_session)
    bases = _bases_as_of(db_session)
    par = (ids["Fighter Alpha"], ids["Fighter Bravo"])
    long = build_long_frame(read_granular(db_session))

    ao_vivo = _asof_matchup_rows(db_session, long, [par], [-1], _HOJE)
    cache = read_asof_features(db_session, list(par))

    assert bases
    for base in bases:
        for sufixo, fighter_id in zip(_SUFIXOS, par, strict=True):
            esperado = ao_vivo[f"{base}{sufixo}"].iloc[0]
            obtido = cache[fighter_id].features[base]
            assert _iguais(esperado, obtido), (base, sufixo, esperado, obtido)


def test_paridade_nivel_2_o_vetor_servido_bate_coluna_a_coluna(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-03: a matriz montada do cache, alinhada a ``feature_names``, é a de hoje.

    Nível que amarra o que o modelo **come**: inclui o D3 derivado do ``style_history``, os
    diferenciais ``*_diff`` e as colunas que degradam para ``NaN`` (o contexto bout-level que
    o serving declaradamente não reconstrói -- CA-16 da SPEC 009). ``NaN`` casa com ``NaN``:
    a degradação tem de ser **a mesma** nos dois caminhos, não só "existir" em ambos.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    _materializar(db_session)
    loaded = load_artifact(tmp_path)
    pares = [
        (ids["Fighter Alpha"], ids["Fighter Delta"]),
        (ids["Fighter Bravo"], ids["Fighter Charlie"]),
    ]
    bout_ids = [-1, -2]
    long = build_long_frame(read_granular(db_session))
    cache = read_asof_features(db_session, [fighter_id for par in pares for fighter_id in par])

    ao_vivo = _asof_matchup_rows(db_session, long, pares, bout_ids, _HOJE)
    do_cache = _cached_matchup_rows(cache, pares, bout_ids)

    # A ordem de saída do pivô não é contrato: alinhar por ``bout_id`` antes de comparar.
    ao_vivo = ao_vivo.sort_values(COL_BOUT_ID).reset_index(drop=True)
    do_cache = do_cache.sort_values(COL_BOUT_ID).reset_index(drop=True)
    esperado = _align_features(ao_vivo, loaded.feature_names)
    obtido = _align_features(do_cache, loaded.feature_names)

    assert list(obtido.columns) == list(esperado.columns) == loaded.feature_names
    assert len(obtido) == len(esperado) == len(pares)
    for coluna in loaded.feature_names:
        for linha in range(len(pares)):
            assert _iguais(esperado[coluna].iloc[linha], obtido[coluna].iloc[linha]), (
                coluna,
                linha,
            )


def test_paridade_nivel_2_cobre_o_d3_derivado_do_style_history(
    db_session: Session, tmp_path: Path
) -> None:
    """Guarda contra vacuidade do nível 2: o D3 **está** entre as colunas comparadas.

    Sem esta asserção, o nível 2 continuaria verde se o D3 sumisse de ``feature_names`` -- e a
    feature mais delicada da slice (a única do PAR) deixaria de ser verificada em silêncio.
    """
    _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    nomes = load_artifact(tmp_path).feature_names

    for sufixo in ("_a", "_b", "_diff"):
        assert f"{SIMILAR_STYLE_WIN_RATE_PRIOR}{sufixo}" in nomes, sufixo


def test_invariante_features_de_a_nao_dependem_da_identidade_de_b(db_session: Session) -> None:
    """A premissa que torna o cache por lutador correto, fixada por teste.

    Trocar o adversário da luta sintética não pode mover **nenhuma** base as-of do lado A.
    A única coluna autorizada a mudar é ``similar_style_win_rate_prior``, que é feature do PAR
    -- e por isso mesmo não entra no payload cacheado (D-15).
    """
    ids = _seed_history(db_session)
    bases = _bases_as_of(db_session)
    long = build_long_frame(read_granular(db_session))
    alpha = ids["Fighter Alpha"]

    contra_delta = _asof_matchup_rows(
        db_session, long, [(alpha, ids["Fighter Delta"])], [-1], _HOJE
    )
    contra_charlie = _asof_matchup_rows(
        db_session, long, [(alpha, ids["Fighter Charlie"])], [-1], _HOJE
    )

    assert bases
    for base in bases:
        coluna = f"{base}_a"
        assert _iguais(contra_delta[coluna].iloc[0], contra_charlie[coluna].iloc[0]), base


def test_invariante_o_d3_e_a_excecao_declarada(db_session: Session) -> None:
    """...e o D3 **de fato** depende do adversário -- por isso é derivado no confronto.

    Contraprova da invariante anterior: alimentado com o ``style_history`` cacheado de A e com
    eixos de adversário opostos, o mesmo histórico produz priors diferentes. É exatamente o
    motivo de ``similar_style_win_rate_prior`` viver fora do vetor por lutador.
    """
    ids = _seed_history(db_session)
    _materializar(db_session)
    # Bravo, e não Alpha: no roster sintético o de maior alcance vence tudo, e um histórico
    # de 100% de vitórias daria prior ``1.0`` para qualquer ponderação -- o teste passaria
    # medindo a saturação, não a dependência do adversário. Bravo perde para Alpha e vence os
    # demais, então os pesos de fato movem a média.
    bravo = ids["Fighter Bravo"]

    historico = pd.DataFrame(read_asof_features(db_session, [bravo])[bravo].style_history)
    grappler = similar_style_prior_for_opponent(historico, 1.0, 0.0)
    volumoso = similar_style_prior_for_opponent(historico, 0.0, 1.0)

    assert grappler is not None
    assert volumoso is not None
    assert not math.isclose(grappler, volumoso, abs_tol=_TOLERANCIA)
