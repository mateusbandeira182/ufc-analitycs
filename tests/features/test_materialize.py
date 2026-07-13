"""Testes da materialização da matriz de confronto em ``bout_features`` -- Plano 005-05.

Cobrem a Slice 05 (SPEC 005, M4): persistir o cache reconstrutível ``bout_features`` a
partir da ``MatchupMatrix`` da Slice 04, via upsert idempotente por ``bout_id``. As
asserções são sobre o **estado do banco** (o sinal demonstrável da ingestão), contra o
Postgres de teste transacional.

Reconciliação com o contrato real da Slice 04: ``materialize_features`` recebe a
``MatchupMatrix`` (dataclass com ``frame`` + ``feature_columns`` + ``target_column``), não
um ``DataFrame`` cru. O snippet ilustrativo do plano derivava as features excluindo apenas
``bout_id``/``winner_corner`` -- o que despejaria identidade/contexto/desfecho (``result_a``,
``method_a``, ``fighter_id_a``...) no JSONB e vazaria. A ``MatchupMatrix`` já carrega a lista
exata de ``feature_columns`` (``*_a``/``*_b``/``*_diff``), que é a fonte da verdade do que é
feature. O alvo ``winner_corner`` chega como ``"R"``/``"B"`` (matchup) e é mapeado para o
enum ``Corner`` (``red``/``blue``) na borda.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.features.models import BoutFeatures
from apps.fighters.models import Fighter
from ingestion.features.cli import run_materialize
from ingestion.features.matchup import MatchupMatrix
from ingestion.features.materialize import SOURCE, _to_corner, materialize_features
from ingestion.features.rolling import ROUND1_SIG_STRIKE_SHARE_R3, SHARE_HEAD_R3
from ingestion.normalize import normalize_name

_FEATURE_COLUMNS = ["sig_strikes_pm_asof_a", "sig_strikes_pm_asof_b", "sig_strikes_pm_asof_diff"]


def _seed_bout(db_session: Session, *, decided: bool = True) -> int:
    """Semeia uma luta com dois cantos e devolve o ``bout_id`` (FK de ``bout_features``).

    ``decided=True`` define um vencedor (KO/TKO); ``decided=False`` deixa ``winner_id``
    nulo (empate/no contest), útil para o alvo nulo.
    """
    red, blue = (
        Fighter(
            name=name,
            name_normalized=normalize_name(name),
            nickname=None,
            date_of_birth=None,
            height_cm=None,
            reach_cm=None,
            stance=None,
            wins=0,
            losses=0,
            draws=0,
            source="kaggle",
        )
        for name in ("Red Corner", "Blue Corner")
    )
    db_session.add_all([red, blue])
    event = Event(name="UFC Test", date=date(2024, 1, 1), location=None, source="kaggle")
    db_session.add(event)
    db_session.flush()
    bout = Bout(
        event_id=event.id,
        winner_id=red.id if decided else None,
        method=BoutMethod.KO_TKO if decided else BoutMethod.NO_CONTEST,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
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
                sig_strikes_landed=None,
                sig_strikes_attempted=None,
                takedowns_landed=None,
                takedowns_attempted=None,
                submission_attempts=None,
                control_time_seconds=None,
                source="kaggle",
            ),
            BoutFighter(
                bout_id=bout.id,
                fighter_id=blue.id,
                corner=Corner.BLUE,
                knockdowns=None,
                sig_strikes_landed=None,
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


def _matrix(rows: list[dict[str, object]]) -> MatchupMatrix:
    """Monta uma ``MatchupMatrix`` mínima a partir de linhas bout-level à mão."""
    frame = pd.DataFrame(rows)
    return MatchupMatrix(
        frame=frame,
        feature_columns=_FEATURE_COLUMNS,
        target_column="winner_corner",
        excluded_no_result=0,
        red_corner_win_rate=0.5,
    )


def test_materialize_grava_uma_linha_por_bout_com_features_e_alvo(db_session: Session) -> None:
    """CA-02: uma linha por bout; features JSONB sem o alvo; ``winner_corner`` -> enum."""
    bout_id = _seed_bout(db_session)
    matrix = _matrix(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": 4.0,
                "sig_strikes_pm_asof_b": 3.0,
                "sig_strikes_pm_asof_diff": 1.0,
                "winner_corner": "R",
            }
        ]
    )

    inserted = materialize_features(db_session, matrix)

    assert inserted == 1
    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    assert row.features == {
        "sig_strikes_pm_asof_a": 4.0,
        "sig_strikes_pm_asof_b": 3.0,
        "sig_strikes_pm_asof_diff": 1.0,
    }
    # O alvo é separado das features (não entra no JSONB) e mapeado para o enum Corner.
    assert "winner_corner" not in row.features
    assert row.target_winner_corner is Corner.RED


def test_materialize_registra_source_e_generated_at_tz_aware(db_session: Session) -> None:
    """CA-03: cada linha carimba ``source`` e ``generated_at`` timezone-aware em UTC."""
    bout_id = _seed_bout(db_session)
    antes = datetime.now(UTC)
    matrix = _matrix(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": 1.0,
                "sig_strikes_pm_asof_b": 2.0,
                "sig_strikes_pm_asof_diff": -1.0,
                "winner_corner": "B",
            }
        ]
    )

    materialize_features(db_session, matrix)

    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    assert row.source == SOURCE
    assert row.generated_at.tzinfo is not None
    assert row.generated_at.utcoffset() == UTC.utcoffset(None)
    assert row.generated_at >= antes


def test_materialize_nan_vira_null_explicito_no_jsonb(db_session: Session) -> None:
    """CA-03: ``NaN``/``NA`` do Pandas viram ``null`` explícito no JSONB (estreia sem histórico)."""
    bout_id = _seed_bout(db_session)
    matrix = _matrix(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": float("nan"),
                "sig_strikes_pm_asof_b": pd.NA,
                "sig_strikes_pm_asof_diff": 2.0,
                "winner_corner": "R",
            }
        ]
    )

    materialize_features(db_session, matrix)

    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    assert row.features["sig_strikes_pm_asof_a"] is None
    assert row.features["sig_strikes_pm_asof_b"] is None
    assert row.features["sig_strikes_pm_asof_diff"] == 2.0


def test_materialize_alvo_nulo_para_nc_draw(db_session: Session) -> None:
    """CA-02/CA-03: um bout sem vencedor definido (``winner_corner`` NA) grava alvo nulo."""
    bout_id = _seed_bout(db_session, decided=False)
    matrix = _matrix(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": 1.0,
                "sig_strikes_pm_asof_b": 1.0,
                "sig_strikes_pm_asof_diff": 0.0,
                "winner_corner": pd.NA,
            }
        ]
    )

    materialize_features(db_session, matrix)

    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    assert row.target_winner_corner is None


def test_materialize_converte_numpy_para_tipos_nativos(db_session: Session) -> None:
    """CA-03: tipos numpy/pandas (``Int64``) viram tipos Python nativos serializáveis em JSON."""
    bout_id = _seed_bout(db_session)
    frame = pd.DataFrame(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": 4.0,
                "sig_strikes_pm_asof_b": 3.0,
                "sig_strikes_pm_asof_diff": 1,
                "winner_corner": "R",
            }
        ]
    )
    # Coluna de diferencial como inteiro nullable (Int64) -- espelha o dtype real do matchup.
    frame["sig_strikes_pm_asof_diff"] = frame["sig_strikes_pm_asof_diff"].astype("Int64")
    matrix = MatchupMatrix(
        frame=frame,
        feature_columns=_FEATURE_COLUMNS,
        target_column="winner_corner",
        excluded_no_result=0,
        red_corner_win_rate=1.0,
    )

    materialize_features(db_session, matrix)

    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    valor = row.features["sig_strikes_pm_asof_diff"]
    assert valor == 1
    assert type(valor) is int  # tipo Python nativo, não numpy/pandas


def _snapshot_sem_generated_at(db_session: Session) -> dict[int, tuple[object, object, str]]:
    """Conteúdo determinístico de ``bout_features`` (features/alvo/source), sem ``generated_at``."""
    linhas = db_session.execute(select(BoutFeatures)).scalars().all()
    return {
        linha.bout_id: (linha.features, linha.target_winner_corner, linha.source)
        for linha in linhas
    }


def test_materialize_e_idempotente_e_nao_toca_o_granular(db_session: Session) -> None:
    """CA-05: rodar 2x mantém contagem e conteúdo; ``bouts``/``bout_fighters`` intocados."""
    bout_id = _seed_bout(db_session)
    matrix = _matrix(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": 4.0,
                "sig_strikes_pm_asof_b": 3.0,
                "sig_strikes_pm_asof_diff": 1.0,
                "winner_corner": "R",
            }
        ]
    )
    bouts_antes = db_session.scalar(select(func.count()).select_from(Bout))
    bout_fighters_antes = db_session.scalar(select(func.count()).select_from(BoutFighter))

    materialize_features(db_session, matrix)
    primeiro = _snapshot_sem_generated_at(db_session)
    materialize_features(db_session, matrix)
    segundo = _snapshot_sem_generated_at(db_session)

    # Mesma contagem e mesmo conteúdo (excluindo generated_at, que pode ser refrescado).
    assert db_session.scalar(select(func.count()).select_from(BoutFeatures)) == 1
    assert primeiro == segundo
    # Granular intocado: nenhuma escrita/alteração em bouts/bout_fighters.
    assert db_session.scalar(select(func.count()).select_from(Bout)) == bouts_antes
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == bout_fighters_antes


def test_materialize_atualiza_conteudo_no_reprocessamento(db_session: Session) -> None:
    """CA-05: um rebuild com features diferentes atualiza a linha (upsert), sem duplicar."""
    bout_id = _seed_bout(db_session)
    base_row: dict[str, object] = {
        "bout_id": bout_id,
        "sig_strikes_pm_asof_a": 4.0,
        "sig_strikes_pm_asof_b": 3.0,
        "sig_strikes_pm_asof_diff": 1.0,
        "winner_corner": "R",
    }

    materialize_features(db_session, _matrix([base_row]))
    atualizado = {**base_row, "sig_strikes_pm_asof_diff": 9.0}
    materialize_features(db_session, _matrix([atualizado]))

    assert db_session.scalar(select(func.count()).select_from(BoutFeatures)) == 1
    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    db_session.refresh(row)
    assert row.features["sig_strikes_pm_asof_diff"] == 9.0


def test_run_materialize_roda_pipeline_completa_e_popula_bout_features(
    db_session: Session,
) -> None:
    """CA-04: ``run_materialize`` roda a pipeline (long->rolling->trajectory->matchup) e persiste.

    Semeia uma luta decidida e confirma que a tabela derivada é populada com uma linha por
    bout decidido, com o alvo mapeado -- observável por consulta ao banco (sinal da ingestão).
    """
    bout_id = _seed_bout(db_session)

    inseridas = run_materialize(db_session)

    assert inseridas == 1
    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    assert row.source == SOURCE
    assert row.target_winner_corner is Corner.RED
    # Rodar de novo é idempotente: mesma contagem (upsert por bout_id).
    run_materialize(db_session)
    assert db_session.scalar(select(func.count()).select_from(BoutFeatures)) == 1


def _seed_fighter(db_session: Session, name: str) -> Fighter:
    """Semeia um lutador mínimo e devolve o model já com id."""
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


def _seed_two_bouts_with_splits_and_rounds(db_session: Session) -> int:
    """Semeia o lutador A com duas lutas (splits + round-a-round na 1a); devolve o bout_id da 2a.

    A vence as duas por decisão contra oponentes distintos, splits preenchidos nos dois
    cantos de A, e round-a-round só na 1a luta (r1=15, r2=5 -> round1 share 0.75). A 2a luta
    passa a ter features as-of de perfil de striking e de dinâmica por round.
    """
    a = _seed_fighter(db_session, "Fighter A")
    b = _seed_fighter(db_session, "Opponent B")
    c = _seed_fighter(db_session, "Opponent C")
    evt1 = Event(name="UFC 1: Test", date=date(2023, 1, 1), location=None, source="kaggle")
    evt2 = Event(name="UFC 2: Test", date=date(2023, 6, 1), location=None, source="kaggle")
    db_session.add_all([evt1, evt2])
    db_session.flush()

    second_bout_id = 0
    for event, opponent in ((evt1, b), (evt2, c)):
        bout = Bout(
            event_id=event.id,
            winner_id=a.id,
            method=BoutMethod.DECISION,
            round=3,
            ending_time_seconds=300,
            weight_class=None,
            source="kaggle",
        )
        db_session.add(bout)
        db_session.flush()
        second_bout_id = int(bout.id)
        a_corner = BoutFighter(
            bout_id=bout.id,
            fighter_id=a.id,
            corner=Corner.RED,
            knockdowns=None,
            sig_strikes_landed=30,
            sig_strikes_attempted=None,
            takedowns_landed=None,
            takedowns_attempted=None,
            submission_attempts=None,
            control_time_seconds=None,
            total_strikes_landed=30,
            total_strikes_attempted=None,
            head_landed=20,
            head_attempted=None,
            body_landed=5,
            body_attempted=None,
            leg_landed=5,
            leg_attempted=None,
            distance_landed=18,
            distance_attempted=None,
            clinch_landed=6,
            clinch_attempted=None,
            ground_landed=6,
            ground_attempted=None,
            reversals=None,
            source="kaggle",
        )
        db_session.add_all(
            [
                a_corner,
                BoutFighter(
                    bout_id=bout.id,
                    fighter_id=opponent.id,
                    corner=Corner.BLUE,
                    knockdowns=None,
                    sig_strikes_landed=10,
                    sig_strikes_attempted=None,
                    takedowns_landed=None,
                    takedowns_attempted=None,
                    submission_attempts=None,
                    control_time_seconds=None,
                    total_strikes_landed=None,
                    total_strikes_attempted=None,
                    head_landed=None,
                    head_attempted=None,
                    body_landed=None,
                    body_attempted=None,
                    leg_landed=None,
                    leg_attempted=None,
                    distance_landed=None,
                    distance_attempted=None,
                    clinch_landed=None,
                    clinch_attempted=None,
                    ground_landed=None,
                    ground_attempted=None,
                    reversals=None,
                    source="kaggle",
                ),
            ]
        )
        db_session.flush()
        if event is evt1:
            db_session.add_all(
                [
                    BoutFighterRound(
                        bout_fighter_id=a_corner.id,
                        round=rnd,
                        knockdowns=None,
                        sig_strikes_landed=sig,
                        sig_strikes_attempted=None,
                        takedowns_landed=None,
                        takedowns_attempted=None,
                        submission_attempts=None,
                        control_time_seconds=None,
                        total_strikes_landed=None,
                        total_strikes_attempted=None,
                        head_landed=None,
                        head_attempted=None,
                        body_landed=None,
                        body_attempted=None,
                        leg_landed=None,
                        leg_attempted=None,
                        distance_landed=None,
                        distance_attempted=None,
                        clinch_landed=None,
                        clinch_attempted=None,
                        ground_landed=None,
                        ground_attempted=None,
                        reversals=None,
                        source="cito",
                    )
                    for rnd, sig in {1: 15, 2: 5}.items()
                ]
            )
            db_session.flush()
    return second_bout_id


def test_run_materialize_grava_features_novas_e_e_idempotente(db_session: Session) -> None:
    """CA-03: as features novas (share/round1) entram no payload; re-materializar é idempotente.

    A 2a luta de A tem perfil de striking as-of (``share_head_r3_a`` = 20/30) e dinâmica por
    round (``round1_sig_strike_share_r3_a`` = 0.75). Re-executar mantém a contagem (upsert por
    ``bout_id``) e ``NaN`` vira ``None`` no JSONB (nunca ``inf``).
    """
    second_bout_id = _seed_two_bouts_with_splits_and_rounds(db_session)

    run_materialize(db_session)
    count_primeiro = db_session.scalar(select(func.count()).select_from(BoutFeatures))

    row = db_session.get(BoutFeatures, second_bout_id)
    assert row is not None
    # As features novas estão no payload da 2a luta (as-of, só a 1a como histórico).
    assert row.features[f"{SHARE_HEAD_R3}_a"] == pytest.approx(20 / 30)
    assert row.features[f"{ROUND1_SIG_STRIKE_SHARE_R3}_a"] == pytest.approx(0.75)
    # Os splits raw da luta corrente NÃO vazam para o payload (anti-leakage).
    assert "head_landed_a" not in row.features
    assert "reversals_a" not in row.features
    # NaN -> None explícito (o canto oposto é estreia -> as-of ausente).
    assert row.features[f"{SHARE_HEAD_R3}_b"] is None
    # inf jamais entra no JSONB.
    for valor in row.features.values():
        assert valor != float("inf")

    # Re-materializar mantém a contagem (idempotência por bout_id).
    run_materialize(db_session)
    count_segundo = db_session.scalar(select(func.count()).select_from(BoutFeatures))
    assert count_primeiro == count_segundo


def test_to_corner_valor_inesperado_levanta_value_error_claro() -> None:
    """Alvo fora de ``R``/``B`` falha com ``ValueError`` claro (não ``KeyError`` sem contexto)."""
    assert _to_corner("R") is Corner.RED
    assert _to_corner("B") is Corner.BLUE
    assert _to_corner(None) is None
    with pytest.raises(ValueError, match="inesperado"):
        _to_corner("X")
