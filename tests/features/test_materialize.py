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
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.features.models import BoutFeatures
from apps.fighters.models import Fighter
from ingestion.features.cli import run_materialize
from ingestion.features.matchup import MatchupMatrix
from ingestion.features.materialize import SOURCE, _to_corner, materialize_features
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


def test_to_corner_valor_inesperado_levanta_value_error_claro() -> None:
    """Alvo fora de ``R``/``B`` falha com ``ValueError`` claro (não ``KeyError`` sem contexto)."""
    assert _to_corner("R") is Corner.RED
    assert _to_corner("B") is Corner.BLUE
    assert _to_corner(None) is None
    with pytest.raises(ValueError, match="inesperado"):
        _to_corner("X")
