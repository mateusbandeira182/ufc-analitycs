"""Testes da guarda de defasagem de ``bout_features`` -- Plano 007-01 (CA-03).

Cobrem a Slice 01 da SPEC 007: uma verificação que **falha alto** quando o cache derivado
``bout_features`` está defasado em relação ao granular vivo (``bouts``/``bout_fighters``).
A defasagem foi a falha silenciosa que motivou a sprint -- o backfill do M5 escreveu os
splits no granular e ninguém re-materializou o cache, então o treino descartava 21 colunas
100% nulas com um ``logger.warning`` que ninguém leu.

As asserções são sobre o **estado do banco** (o sinal demonstrável da ingestão), contra o
Postgres de teste transacional da fixture ``db_session``. Cada teste cobre um dos três
sinais de defasagem, mais o caminho feliz do cache recém-materializado e a superfície de
CLI (``run_check``).

Por que **não** comparar valores de feature: float round-trip por JSONB não é seguro para
igualdade. A guarda compara conjuntos de ``bout_id``, conjuntos de chaves de feature e
contagens de não-nulos por coluna -- o suficiente para pegar o sintoma real (coluna com 0
não-nulos no cache e milhares no recomputo) com diagnóstico legível.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.features.models import BoutFeatures
from apps.fighters.models import Fighter
from ingestion.features.cli import _enriched_long_frame, run_check, run_materialize
from ingestion.features.division import (
    DIVISION_FINISH_RATE_PRIOR,
    IS_WOMENS_DIVISION,
    WEIGHT_CLASS_LBS,
)
from ingestion.features.freshness import (
    StaleFeatureCacheError,
    check_cache_freshness,
    read_cached_payloads,
)
from ingestion.features.matchup import (
    IS_TITLE_BOUT,
    SCHEDULED_ROUNDS,
    MatchupMatrix,
    build_matchup_matrix,
)
from ingestion.features.rolling import (
    FIVE_ROUND_BOUTS_BEFORE,
    KNOCKDOWNS_PM_R3,
    SHARE_HEAD_R3,
    SIG_STRIKE_ACCURACY_R3,
)
from ingestion.normalize import normalize_name

# Splits de golpe conectado do canto vencedor: alimentam o perfil de striking as-of do M5.
_SPLITS: dict[str, int] = {
    "total_strikes_landed": 30,
    "head_landed": 20,
    "body_landed": 5,
    "leg_landed": 5,
    "distance_landed": 18,
    "clinch_landed": 6,
    "ground_landed": 6,
}


def _seed_fighter(db_session: Session, name: str) -> Fighter:
    """Semeia um lutador mínimo (com data de nascimento) e devolve o model já com id."""
    fighter = Fighter(
        name=name,
        name_normalized=normalize_name(name),
        nickname=None,
        date_of_birth=date(1990, 1, 1),
        height_cm=None,
        reach_cm=None,
        stance=None,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
    )
    db_session.add(fighter)
    db_session.flush()
    return fighter


def _seed_bout(
    db_session: Session,
    *,
    event: Event,
    red: Fighter,
    blue: Fighter,
    splits: dict[str, int] | None,
) -> int:
    """Semeia uma luta decidida (vitória do canto vermelho) e devolve o ``bout_id``.

    ``splits=None`` deixa os splits de golpe nulos -- estado do granular antes do backfill
    do M5. Com ``splits`` preenchidos, o perfil de striking as-of passa a existir para as
    lutas seguintes do mesmo lutador.
    """
    bout = Bout(
        event_id=event.id,
        winner_id=red.id,
        method=BoutMethod.DECISION,
        round=3,
        ending_time_seconds=300,
        weight_class="lightweight",
        title_bout=False,
        scheduled_rounds=3,
        source="kaggle",
    )
    db_session.add(bout)
    db_session.flush()
    db_session.add_all(
        [
            BoutFighter(
                bout_id=bout.id,
                fighter_id=red.id,
                corner=Corner.RED,
                knockdowns=None,
                sig_strikes_landed=30,
                sig_strikes_attempted=None,
                takedowns_landed=None,
                takedowns_attempted=None,
                submission_attempts=None,
                control_time_seconds=None,
                **(splits or {}),
                source="kaggle",
            ),
            BoutFighter(
                bout_id=bout.id,
                fighter_id=blue.id,
                corner=Corner.BLUE,
                knockdowns=None,
                sig_strikes_landed=10,
                sig_strikes_attempted=None,
                takedowns_landed=None,
                takedowns_attempted=None,
                submission_attempts=None,
                control_time_seconds=None,
                source="kaggle",
            ),
        ]
    )
    db_session.flush()
    return int(bout.id)


def _seed_two_bouts(db_session: Session, *, splits: dict[str, int] | None) -> tuple[int, int]:
    """Semeia o lutador A com duas lutas vencidas; devolve ``(primeiro, segundo)`` bout_id.

    A segunda luta é a que carrega features as-of (a primeira é estreia dos dois cantos).
    """
    a = _seed_fighter(db_session, "Fighter A")
    b = _seed_fighter(db_session, "Opponent B")
    c = _seed_fighter(db_session, "Opponent C")
    first_event = Event(name="UFC 1: Test", date=date(2023, 1, 1), location=None, source="kaggle")
    second_event = Event(name="UFC 2: Test", date=date(2023, 6, 1), location=None, source="kaggle")
    db_session.add_all([first_event, second_event])
    db_session.flush()
    first = _seed_bout(db_session, event=first_event, red=a, blue=b, splits=splits)
    second = _seed_bout(db_session, event=second_event, red=a, blue=c, splits=splits)
    return first, second


def _fill_splits_in_granular(db_session: Session, bout_id: int) -> None:
    """Preenche os splits de golpe do canto vermelho **depois** da materialização.

    Reproduz o mecanismo da causa raiz: o backfill do M5 escreveu no granular e o cache
    derivado ficou para trás, porque nada aciona a re-materialização.
    """
    stmt = select(BoutFighter).where(
        BoutFighter.bout_id == bout_id, BoutFighter.corner == Corner.RED
    )
    bout_fighter = db_session.execute(stmt).scalar_one()
    for coluna, valor in _SPLITS.items():
        setattr(bout_fighter, coluna, valor)
    db_session.flush()


def _current_matrix(db_session: Session) -> MatchupMatrix:
    """Recomputa a matriz de confronto contra o granular vivo (read-only).

    Reusa ``_enriched_long_frame`` -- a mesma pipeline que ``run_materialize`` persiste.
    Montar a frame à mão aqui divergiria do cache (o estágio de dinâmica por round ficaria
    de fora) e a guarda acusaria uma defasagem que só existe no teste.
    """
    return build_matchup_matrix(_enriched_long_frame(db_session))


def test_check_cache_freshness_cache_recem_materializado_nao_esta_defasado(
    db_session: Session,
) -> None:
    """CA-03: cache materializado a partir do granular vivo não acusa nenhuma defasagem."""
    _seed_two_bouts(db_session, splits=_SPLITS)
    run_materialize(db_session)

    report = check_cache_freshness(db_session, _current_matrix(db_session))

    assert report.is_stale is False
    assert report.bout_id_diff == ()
    assert report.column_diff == ()
    assert report.column_drift == ()
    assert report.cached_bouts == report.recomputed_bouts == 2


def test_check_cache_freshness_acusa_column_drift_quando_granular_ganha_splits(
    db_session: Session,
) -> None:
    """CA-03: splits escritos no granular após a materialização acusam ``column_drift``.

    É exatamente o sintoma observado em produção: ``share_head_r3_a`` com 0 não-nulos no
    cache persistido e valores no recomputo contra o granular vivo.
    """
    first_bout_id, _ = _seed_two_bouts(db_session, splits=None)
    run_materialize(db_session)
    _fill_splits_in_granular(db_session, first_bout_id)

    report = check_cache_freshness(db_session, _current_matrix(db_session))

    assert report.is_stale is True
    assert report.bout_id_diff == ()
    assert report.column_diff == ()
    drifts = {drift.column: drift for drift in report.column_drift}
    share_head_a = drifts[f"{SHARE_HEAD_R3}_a"]
    assert share_head_a.cached_non_null == 0
    assert share_head_a.recomputed_non_null > 0


def test_check_cache_freshness_acusa_bout_id_diff_com_luta_nova_no_granular(
    db_session: Session,
) -> None:
    """CA-03: uma luta ingerida após a materialização acusa ``bout_id_diff``."""
    _seed_two_bouts(db_session, splits=_SPLITS)
    run_materialize(db_session)
    novo_evento = Event(name="UFC 3: Test", date=date(2023, 9, 1), location=None, source="kaggle")
    db_session.add(novo_evento)
    db_session.flush()
    novo_bout_id = _seed_bout(
        db_session,
        event=novo_evento,
        red=_seed_fighter(db_session, "Fighter D"),
        blue=_seed_fighter(db_session, "Opponent E"),
        splits=None,
    )

    report = check_cache_freshness(db_session, _current_matrix(db_session))

    assert report.is_stale is True
    assert report.bout_id_diff == (novo_bout_id,)
    assert report.cached_bouts == 2
    assert report.recomputed_bouts == 3


def test_check_cache_freshness_acusa_column_diff_com_chave_ausente_no_payload(
    db_session: Session,
) -> None:
    """CA-03: chave de feature presente só de um lado acusa ``column_diff``.

    Simula a pipeline ter ganhado uma feature depois da última materialização, removendo a
    chave do payload JSONB persistido.
    """
    _seed_two_bouts(db_session, splits=_SPLITS)
    run_materialize(db_session)
    coluna = f"{SHARE_HEAD_R3}_a"
    db_session.execute(
        text("UPDATE bout_features SET features = features - :chave"), {"chave": coluna}
    )
    db_session.flush()

    report = check_cache_freshness(db_session, _current_matrix(db_session))

    assert report.is_stale is True
    assert report.column_diff == (coluna,)


def test_read_cached_payloads_devolve_um_payload_por_bout(db_session: Session) -> None:
    """A leitura do cache devolve o payload JSONB indexado por ``bout_id``."""
    _, second_bout_id = _seed_two_bouts(db_session, splits=_SPLITS)
    run_materialize(db_session)

    payloads = read_cached_payloads(db_session)

    assert set(payloads) == {
        linha.bout_id for linha in db_session.execute(select(BoutFeatures)).scalars()
    }
    assert payloads[second_bout_id][f"{SHARE_HEAD_R3}_a"] == pytest.approx(20 / 30)


def test_run_check_devolve_relatorio_com_cache_vivo(db_session: Session) -> None:
    """CA-03: com o cache recém-materializado, ``run_check`` devolve o relatório sem levantar."""
    _seed_two_bouts(db_session, splits=_SPLITS)
    run_materialize(db_session)

    report = run_check(db_session)

    assert report.is_stale is False
    assert report.cached_bouts == 2


def test_run_check_levanta_stale_feature_cache_error_com_cache_defasado(
    db_session: Session,
) -> None:
    """CA-03: com o cache defasado, ``run_check`` falha alto com erro tipado e acionável."""
    first_bout_id, _ = _seed_two_bouts(db_session, splits=None)
    run_materialize(db_session)
    _fill_splits_in_granular(db_session, first_bout_id)

    with pytest.raises(StaleFeatureCacheError, match="materialize"):
        run_check(db_session)


def test_run_check_nomeia_as_colunas_defasadas_e_volta_a_passar_apos_materializar(
    db_session: Session,
) -> None:
    """CA-07 da SPEC 009 (RF-08): a falha **nomeia** as colunas e some após re-materializar.

    Simula o estado que a Slice 01 produz no banco real: o cache foi materializado antes de a
    pipeline ganhar as features do bloco A, então as chaves novas existem só no recomputo.
    Contar as divergências não basta -- quem lê o erro precisa saber **quais** colunas
    faltam, senão descobrir o que envelheceu exige reproduzir a pipeline à mão. Depois de
    ``run_materialize``, o mesmo ``run_check`` passa sem levantar.
    """
    _seed_two_bouts(db_session, splits=_SPLITS)
    run_materialize(db_session)
    ausentes = [f"{SIG_STRIKE_ACCURACY_R3}_a", f"{KNOCKDOWNS_PM_R3}_diff"]
    for coluna in ausentes:
        db_session.execute(
            text("UPDATE bout_features SET features = features - :chave"), {"chave": coluna}
        )
    db_session.flush()

    with pytest.raises(StaleFeatureCacheError) as excinfo:
        run_check(db_session)

    mensagem = str(excinfo.value)
    for coluna in ausentes:
        assert coluna in mensagem, coluna
    assert "materialize" in mensagem

    run_materialize(db_session)
    assert run_check(db_session).is_stale is False


def test_run_check_nomeia_as_colunas_de_divisao_e_volta_a_passar_apos_materializar(
    db_session: Session,
) -> None:
    """CA-11 da SPEC 009 (RF-08): a guarda acusa as bout-level novas e some após materializar.

    Reproduz o estado que esta slice produz no banco real: o cache foi materializado antes de
    a pipeline ganhar as features de divisão, então as três chaves existem só no recomputo.
    Elas são **coluna única** (sem sufixo de canto), o que torna este teste também uma guarda
    de que o caminho bout-level chega intacto ao payload persistido -- se o colapso voltasse a
    produzir par degenerado, as chaves nomeadas aqui seriam outras.
    """
    _seed_two_bouts(db_session, splits=_SPLITS)
    run_materialize(db_session)
    ausentes = [WEIGHT_CLASS_LBS, IS_WOMENS_DIVISION, DIVISION_FINISH_RATE_PRIOR]
    for coluna in ausentes:
        db_session.execute(
            text("UPDATE bout_features SET features = features - :chave"), {"chave": coluna}
        )
    db_session.flush()

    with pytest.raises(StaleFeatureCacheError) as excinfo:
        run_check(db_session)

    mensagem = str(excinfo.value)
    for coluna in ausentes:
        assert coluna in mensagem, coluna
    assert "materialize" in mensagem

    run_materialize(db_session)
    assert run_check(db_session).is_stale is False


def test_run_check_nomeia_as_colunas_de_formato_e_volta_a_passar_apos_materializar(
    db_session: Session,
) -> None:
    """CA-11 da SPEC 009 (RF-08): a guarda acusa as 5 colunas do bloco B2 e some depois.

    Reproduz o estado que esta slice produz no banco real: o cache foi materializado antes de
    a pipeline ganhar as features de formato, então as cinco chaves existem só no recomputo.
    Duas delas são **coluna única** (bout-level) e três são o trio de canto, o que torna este
    teste também a guarda de que os dois caminhos chegam intactos ao payload persistido.

    ``scheduled_rounds`` está na lista de propósito: é a chave de nome colidente com a coluna
    crua excluída, e se a precedência da allowlist regredisse ela **não** apareceria no
    recomputo -- o ``check`` passaria em silêncio sobre um cache sem a feature.
    """
    _seed_two_bouts(db_session, splits=_SPLITS)
    run_materialize(db_session)
    ausentes = [
        IS_TITLE_BOUT,
        SCHEDULED_ROUNDS,
        *(f"{FIVE_ROUND_BOUTS_BEFORE}{sufixo}" for sufixo in ("_a", "_b", "_diff")),
    ]
    assert len(ausentes) == 5
    for coluna in ausentes:
        db_session.execute(
            text("UPDATE bout_features SET features = features - :chave"), {"chave": coluna}
        )
    db_session.flush()

    with pytest.raises(StaleFeatureCacheError) as excinfo:
        run_check(db_session)

    mensagem = str(excinfo.value)
    for coluna in ausentes:
        assert coluna in mensagem, coluna
    assert "materialize" in mensagem

    run_materialize(db_session)
    assert run_check(db_session).is_stale is False
