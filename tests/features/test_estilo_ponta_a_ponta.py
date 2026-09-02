"""Teste ponta a ponta dos blocos D1+D2 -- SPEC 009, Slice 05 (CA-10, CA-09).

Contra o Postgres de teste transacional, pelo caminho real (``run_materialize``/``run_check``),
numa sequência só -- que é como a slice é demonstrada:

1. o cache defasado (sem as 12 chaves novas) faz ``run_check`` falhar **nomeando** cada uma;
2. ``run_materialize`` as persiste e o mesmo ``run_check`` volta a passar;
3. as 12 chegam ao payload JSONB como número ou ``null`` -- jamais string, jamais ``NaN``
   (que não é JSON válido) -- e as bout-level chegam como **coluna única**;
4. nenhuma delas é descartada em silêncio por ``build_dataset`` -- o modo de falha que
   escondeu 21 features do M5 por meses;
5. o granular fica intocado: a re-materialização escreve só no cache derivado.

A fixture tem **histórico real dos dois cantos** de propósito: dois lutadores com duas lutas
anteriores cada, com box-score de striking e de grappling preenchido. Com fixture pobre (um
canto sempre estreante, como basta às slices anteriores) as 12 colunas nasceriam 100% ``NaN``,
``_drop_all_nan_feature_columns`` as descartaria e o teste falharia pelo motivo errado --
dizendo "feature descartada" onde o defeito real seria "fixture sem dado".
"""

from __future__ import annotations

import math
from datetime import date

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from analysis.dataset import _numeric_feature_columns, build_dataset, read_bout_features
from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.features.models import BoutFeatures
from apps.fighters.enums import Stance
from apps.fighters.models import Fighter
from ingestion.features.cli import _enriched_long_frame, run_check, run_materialize
from ingestion.features.freshness import StaleFeatureCacheError, check_cache_freshness
from ingestion.features.matchup import (
    COL_STANCE,
    INVOLVES_SWITCH_STANCE,
    IS_OPEN_STANCE_MATCHUP,
    STYLE_DISTANCE,
    MatchupMatrix,
    build_matchup_matrix,
)
from ingestion.features.rolling import (
    GRAPPLING_AXIS_R3,
    GRAPPLING_CONTROL_SECONDS_DIVISOR,
    GRAPPLING_TAKEDOWNS_DIVISOR,
    SOUTHPAW_OPPONENTS_FACED_PRIOR,
    VOLUME_AXIS_R3,
    VOLUME_SIG_STRIKES_PM_DIVISOR,
)
from ingestion.normalize import normalize_name

# As 12 colunas que a slice acrescenta ao payload: três bases as-of (trio de canto) e três
# bout-level (coluna única). Montadas a partir das constantes, nunca redigitadas.
_BASES_AS_OF: tuple[str, ...] = (
    SOUTHPAW_OPPONENTS_FACED_PRIOR,
    GRAPPLING_AXIS_R3,
    VOLUME_AXIS_R3,
)
_BOUT_LEVEL: tuple[str, ...] = (IS_OPEN_STANCE_MATCHUP, INVOLVES_SWITCH_STANCE, STYLE_DISTANCE)
_COLUNAS_DA_SLICE: tuple[str, ...] = (
    *(f"{base}{sufixo}" for base in _BASES_AS_OF for sufixo in ("_a", "_b", "_diff")),
    *_BOUT_LEVEL,
)

# Box-score de cada luta anterior do lutador A -- um grappler de volume médio: 1 queda, 150 s
# de controle e 30 golpes significativos em 15 minutos, distribuídos 6/1/3 entre distância,
# clinch e solo.
_STATS_A: dict[str, int] = {
    "sig_strikes_landed": 30,
    "sig_strikes_attempted": 60,
    "takedowns_landed": 1,
    "takedowns_attempted": 2,
    "control_time_seconds": 150,
    "distance_landed": 6,
    "clinch_landed": 1,
    "ground_landed": 3,
}
# Box-score de cada luta anterior do lutador B -- um striker puro de distância: nenhuma queda,
# nenhum controle, 60 golpes significativos em 15 minutos, todos na distância.
_STATS_B: dict[str, int] = {
    "sig_strikes_landed": 60,
    "sig_strikes_attempted": 120,
    "takedowns_landed": 0,
    "takedowns_attempted": 0,
    "control_time_seconds": 0,
    "distance_landed": 10,
    "clinch_landed": 0,
    "ground_landed": 0,
}
# O adversário de cada luta anterior existe, mas sem box-score: o que a slice mede é o estilo
# de A e de B, e um adversário com stats só acrescentaria ruído ao valor conferido à mão.
_SEM_STATS: dict[str, int] = {}

# Minutos de cativeiro de toda luta da fixture: 3 rounds cheios.
_MINUTOS = 15.0

# Vetores de estilo esperados na luta final, derivados das duas lutas anteriores de cada um
# (idênticas entre si, então a média da janela é o valor de uma luta).
_GRAPPLING_A = (
    1.0 / GRAPPLING_TAKEDOWNS_DIVISOR + 150.0 / GRAPPLING_CONTROL_SECONDS_DIVISOR + 0.3
) / 3.0
_VOLUME_A = ((30.0 / _MINUTOS) / VOLUME_SIG_STRIKES_PM_DIVISOR + 0.6) / 2.0
_GRAPPLING_B = 0.0
_VOLUME_B = ((60.0 / _MINUTOS) / VOLUME_SIG_STRIKES_PM_DIVISOR + 1.0) / 2.0
_STYLE_DISTANCE = ((_GRAPPLING_A - _GRAPPLING_B) ** 2 + (_VOLUME_A - _VOLUME_B) ** 2) ** 0.5

_GRANULAR = (Bout, BoutFighter, BoutFighterRound, Fighter, Event)


def _seed_fighter(db_session: Session, name: str, stance: Stance) -> Fighter:
    """Semeia um lutador com base registrada -- ela é o insumo do bloco D1."""
    fighter = Fighter(
        name=name,
        name_normalized=normalize_name(name),
        nickname=None,
        date_of_birth=date(1990, 1, 1),
        height_cm=180,
        reach_cm=180,
        stance=stance,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
    )
    db_session.add(fighter)
    db_session.flush()
    return fighter


def _corner(bout_id: int, fighter: Fighter, corner: Corner, stats: dict[str, int]) -> BoutFighter:
    """Um canto da luta, com o box-score dado (o que estiver fora de ``stats`` fica nulo)."""
    return BoutFighter(
        bout_id=bout_id,
        fighter_id=fighter.id,
        corner=corner,
        knockdowns=0,
        submission_attempts=0,
        total_strikes_landed=stats.get("sig_strikes_landed"),
        source="kaggle",
        **stats,
    )


def _seed_bout(
    db_session: Session,
    *,
    event: Event,
    red: Fighter,
    blue: Fighter,
    red_stats: dict[str, int],
    blue_stats: dict[str, int],
) -> int:
    """Semeia uma luta decidida (vitória do vermelho) de 3 rounds cheios; devolve o id."""
    bout = Bout(
        event_id=event.id,
        winner_id=red.id,
        method=BoutMethod.DECISION,
        round=3,
        ending_time_seconds=300,
        weight_class="Lightweight",
        title_bout=False,
        scheduled_rounds=3,
        source="kaggle",
    )
    db_session.add(bout)
    db_session.flush()
    db_session.add_all(
        [
            _corner(int(bout.id), red, Corner.RED, red_stats),
            _corner(int(bout.id), blue, Corner.BLUE, blue_stats),
        ]
    )
    db_session.flush()
    return int(bout.id)


def _seed_historico_dos_dois_cantos(db_session: Session) -> int:
    """Semeia duas carreiras completas e o confronto final entre elas; devolve o id da final.

    A (ortodoxo, grappler) vence um canhoto e um destro; B (canhoto, striker de distância)
    vence dois destros; e então A enfrenta B. Na luta final os **dois** cantos têm estilo
    as-of definido e histórico de bases conhecido -- é o que faz as 12 colunas da slice
    nascerem com valor em vez de ``NaN``.
    """
    a = _seed_fighter(db_session, "Fighter A", Stance.ORTHODOX)
    b = _seed_fighter(db_session, "Fighter B", Stance.SOUTHPAW)
    canhoto = _seed_fighter(db_session, "Opponent Canhoto", Stance.SOUTHPAW)
    destro_um = _seed_fighter(db_session, "Opponent Destro Um", Stance.ORTHODOX)
    destro_dois = _seed_fighter(db_session, "Opponent Destro Dois", Stance.ORTHODOX)
    destro_tres = _seed_fighter(db_session, "Opponent Destro Tres", Stance.ORTHODOX)

    eventos = [
        Event(name=f"UFC {numero}: Test", date=data, location=None, source="kaggle")
        for numero, data in enumerate(
            [
                date(2023, 1, 1),
                date(2023, 3, 1),
                date(2023, 5, 1),
                date(2023, 7, 1),
                date(2023, 9, 1),
            ],
            start=1,
        )
    ]
    db_session.add_all(eventos)
    db_session.flush()

    for evento, adversario in zip(eventos[:2], (canhoto, destro_um), strict=True):
        _seed_bout(
            db_session,
            event=evento,
            red=a,
            blue=adversario,
            red_stats=_STATS_A,
            blue_stats=_SEM_STATS,
        )
    for evento, adversario in zip(eventos[2:4], (destro_dois, destro_tres), strict=True):
        _seed_bout(
            db_session,
            event=evento,
            red=b,
            blue=adversario,
            red_stats=_STATS_B,
            blue_stats=_SEM_STATS,
        )
    return _seed_bout(
        db_session,
        event=eventos[4],
        red=a,
        blue=b,
        red_stats=_STATS_A,
        blue_stats=_STATS_B,
    )


def _matriz_recomputada(db_session: Session) -> MatchupMatrix:
    """Recomputa a matriz contra o granular vivo, pelo mesmo caminho de ``run_materialize``.

    Montar a frame à mão aqui divergiria do cache (a dinâmica por round ficaria de fora) e a
    guarda acusaria uma defasagem que só existe no teste.
    """
    return build_matchup_matrix(_enriched_long_frame(db_session))


def _remover_do_cache(db_session: Session, colunas: tuple[str, ...]) -> None:
    """Remove as chaves dos payloads persistidos, simulando o cache anterior à slice."""
    for coluna in colunas:
        db_session.execute(
            text("UPDATE bout_features SET features = features - :chave"), {"chave": coluna}
        )
    db_session.flush()


def test_check_falha_nomeando_as_doze_colunas_e_volta_a_passar_apos_materializar(
    db_session: Session,
) -> None:
    """CA-10 (RF-08): a guarda acusa as 12 colunas novas e some após a re-materialização.

    Reproduz o estado que esta slice produz no banco real: o cache foi materializado antes de
    a pipeline ganhar os blocos D1 e D2, então as 12 chaves existem só no recomputo. Três
    delas são **coluna única** (bout-level), o que torna este teste também a guarda de que o
    caminho bout-level chega intacto ao payload -- se o colapso voltasse a produzir par
    degenerado, os nomes acusados seriam outros.
    """
    _seed_historico_dos_dois_cantos(db_session)
    run_materialize(db_session)
    assert len(_COLUNAS_DA_SLICE) == 12
    _remover_do_cache(db_session, _COLUNAS_DA_SLICE)

    relatorio = check_cache_freshness(db_session, _matriz_recomputada(db_session))
    assert set(_COLUNAS_DA_SLICE) <= set(relatorio.column_diff)

    with pytest.raises(StaleFeatureCacheError) as excinfo:
        run_check(db_session)
    mensagem = str(excinfo.value)
    for coluna in _COLUNAS_DA_SLICE:
        assert coluna in mensagem, coluna
    assert "materialize" in mensagem

    run_materialize(db_session)
    assert run_check(db_session).is_stale is False


def test_payload_das_doze_colunas_e_numerico_ou_nulo_e_sobrevive_ao_dataset(
    db_session: Session,
) -> None:
    """CA-10/CA-09: as 12 colunas vão do granular ao treino sem perder nada pelo caminho.

    Na luta final, ortodoxo contra canhoto: ``is_open_stance_matchup`` = 1 e
    ``involves_switch_stance`` = 0. A enfrentou um canhoto entre os dois adversários
    anteriores e B nenhum -- ``southpaw_opponents_faced_prior`` vale 1 e 0, e o ``0`` é valor
    **legítimo** (B tem histórico), distinto de ausência. Os vetores de estilo saem da fórmula
    normativa do bloco D2 e a distância entre eles é conferida contra os mesmos números.

    As bout-level entram como **coluna única**: nem ``_a``/``_b``, nem ``_diff`` degenerado.
    E o granular não é tocado -- a re-materialização escreve só no cache derivado (CA-15 da
    SPEC).
    """
    bout_final = _seed_historico_dos_dois_cantos(db_session)
    contagens_antes = {
        modelo: db_session.scalar(select(func.count()).select_from(modelo)) for modelo in _GRANULAR
    }

    run_materialize(db_session)

    row = db_session.get(BoutFeatures, bout_final)
    assert row is not None
    for coluna in _COLUNAS_DA_SLICE:
        assert coluna in row.features, coluna
        valor = row.features[coluna]
        assert valor is None or isinstance(valor, int | float), coluna
        assert not (isinstance(valor, float) and math.isnan(valor)), coluna
        assert valor != float("inf"), coluna
    for coluna in _BOUT_LEVEL:
        for sufixo in ("_a", "_b", "_diff"):
            assert f"{coluna}{sufixo}" not in row.features, f"{coluna}{sufixo}"
    # A base crua ``stance`` não é feature numérica e não vira coluna única nenhuma.
    assert "stance" not in row.features

    assert row.features[IS_OPEN_STANCE_MATCHUP] == pytest.approx(1.0)
    assert row.features[INVOLVES_SWITCH_STANCE] == pytest.approx(0.0)
    assert row.features[f"{SOUTHPAW_OPPONENTS_FACED_PRIOR}_a"] == pytest.approx(1.0)
    assert row.features[f"{SOUTHPAW_OPPONENTS_FACED_PRIOR}_b"] == pytest.approx(0.0)
    assert row.features[f"{GRAPPLING_AXIS_R3}_a"] == pytest.approx(_GRAPPLING_A)
    assert row.features[f"{GRAPPLING_AXIS_R3}_b"] == pytest.approx(_GRAPPLING_B)
    assert row.features[f"{VOLUME_AXIS_R3}_a"] == pytest.approx(_VOLUME_A)
    assert row.features[f"{VOLUME_AXIS_R3}_b"] == pytest.approx(_VOLUME_B)
    assert row.features[STYLE_DISTANCE] == pytest.approx(_STYLE_DISTANCE)

    # Nenhuma das 12 é descartada em silêncio a caminho do treino.
    dataset = build_dataset(read_bout_features(db_session))
    assert set(_COLUNAS_DA_SLICE) <= set(dataset.feature_names)

    # CA-15: o granular é fonte da verdade e não é escrito por nenhuma slice desta SPEC.
    for modelo, antes in contagens_antes.items():
        assert db_session.scalar(select(func.count()).select_from(modelo)) == antes, modelo


# A allowlist das colunas que ``analysis.dataset._numeric_feature_columns`` pode descartar por
# carregarem string. Montada a partir de ``COL_STANCE`` (nunca redigitada) porque é o mesmo par
# nomeado na docstring dessa função e asserido em ``tests/analysis/test_dataset.py``.
_STRINGS_TOLERADAS_NO_PAYLOAD: frozenset[str] = frozenset(
    f"{COL_STANCE}{sufixo}" for sufixo in ("_a", "_b")
)


def test_so_as_bases_sao_descartadas_por_serem_string_na_frame_enriquecida(
    db_session: Session,
) -> None:
    """RF-02 na frame **enriquecida**: o descarte por string é exatamente ``stance_a``/``stance_b``.

    Extensão de alcance da guarda da RF-02 pedida na revisão da SPEC 009 (recomendação 4). A
    guarda irmã, ``test_toda_coluna_crua_esta_classificada_em_exatamente_uma_categoria``
    (``tests/features/test_matchup.py``), cobre apenas a frame **crua** -- ``LONG_FRAME_COLUMNS``,
    o que ``read_granular`` traz. ``stance`` não entra por ali: nasce depois, em
    ``trajectory.add_physical_attributes``, e por isso escapava daquela guarda. O ponto cego era
    real: uma coluna string nova acrescentada à frame enriquecida por uma slice futura chegaria ao
    payload JSONB e seria descartada em silêncio por ``_numeric_feature_columns``, sem nenhum
    teste ficar vermelho -- o mesmo modo de falha que escondeu 21 features do M5 por meses e que a
    SPEC 009 existe para não repetir.

    O caminho exercitado é o de produção inteiro (``_enriched_long_frame`` ->
    ``build_matchup_matrix``), não uma cadeia remontada no teste: só assim um passo novo dentro de
    ``_enriched_long_frame`` entra automaticamente no alcance da guarda. As colunas comparadas são
    ``feature_columns``, que é literalmente o conjunto de chaves que ``materialize_features``
    grava no JSONB e que ``build_dataset`` submete a ``_numeric_feature_columns``.

    A asserção é de **igualdade**, não de contenção: a exceção conhecida vira allowlist explícita,
    de modo que uma coluna string nova falhe alto -- e que classificar ``stance`` como não-feature
    (o follow-up que a revisão deixou em aberto) também precise passar por aqui, em vez de mudar o
    payload sem que ninguém note.

    A fixture semeia base registrada nos dois cantos de propósito: com ``stance`` nulo o par
    nasceria só com ``NaN``, ``_numeric_feature_columns`` o classificaria como numérico e o teste
    passaria vazio, afirmando o oposto do que descreve.
    """
    _seed_historico_dos_dois_cantos(db_session)

    matriz = _matriz_recomputada(db_session)
    payload = matriz.frame[matriz.feature_columns]
    descartadas = set(payload.columns) - set(_numeric_feature_columns(payload))

    assert descartadas == set(_STRINGS_TOLERADAS_NO_PAYLOAD)
    # Guarda contra vacuidade: as duas precisam mesmo estar no payload carregando string.
    assert set(payload.columns) >= _STRINGS_TOLERADAS_NO_PAYLOAD
