"""Testes do treino do modelo preditivo e da pipeline de ponta a ponta (fase 2).

Cobrem:

- Determinismo: com ``random_state`` fixo, ``train_model`` produz predições idênticas
  em duas execuções sobre o mesmo X/y (função pura sobre DataFrame sintético).
- Pipeline completa (``run_training``): sobre o Postgres de teste transacional, lê
  ``bout_features``, faz o split temporal, treina e avalia modelo e baseline,
  devolvendo um ``TrainingResult`` coeso.
- Persistência (``save_artifact``): o artefato joblib carrega de volta com modelo,
  features e métricas.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import joblib
import pandas as pd
from sqlalchemy.orm import Session

from analysis.model import run_training, save_artifact, train_model
from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout
from apps.events.models import Event
from apps.features.models import BoutFeatures


def _synthetic_xy() -> tuple[pd.DataFrame, pd.Series]:
    """X/y sintético separável, grande o bastante para o treino do HGBC."""
    features = pd.DataFrame(
        {
            "reach_cm_diff": [float(i % 7) - 3 for i in range(60)],
            "win_rate_prior_diff": [float(i % 5) - 2 for i in range(60)],
        }
    )
    target = pd.Series([i % 2 for i in range(60)])
    return features, target


def test_treino_e_deterministico_com_random_state_fixo() -> None:
    """Com ``random_state`` fixo, duas execuções produzem o mesmo modelo (mesmas predições)."""
    features, target = _synthetic_xy()

    primeiro = train_model(features, target, random_state=0)
    segundo = train_model(features, target, random_state=0)

    assert (primeiro.predict(features) == segundo.predict(features)).all()
    proba_a = primeiro.predict_proba(features)
    proba_b = segundo.predict_proba(features)
    assert (proba_a == proba_b).all()


def _seed_bout_features(
    session: Session,
    *,
    event_date: date,
    target: Corner,
    features: dict[str, object],
) -> None:
    """Semeia evento + luta + ``bout_features`` para a pipeline ler."""
    event = Event(
        name=f"UFC {event_date.isoformat()}", date=event_date, location=None, source="kaggle"
    )
    session.add(event)
    session.flush()
    bout = Bout(
        event_id=event.id,
        winner_id=None,
        method=BoutMethod.DECISION,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source="kaggle",
    )
    session.add(bout)
    session.flush()
    session.add(
        BoutFeatures(
            bout_id=bout.id,
            features=features,
            target_winner_corner=target,
            source="feature-engineering",
            generated_at=datetime.now(UTC),
        )
    )
    session.flush()


def _seed_many(session: Session, n: int) -> None:
    """Semeia ``n`` lutas com datas crescentes e alvos alternados (ambas as classes)."""
    for i in range(n):
        _seed_bout_features(
            session,
            event_date=date(2018, 1, 1) + pd.Timedelta(days=30 * i),
            target=Corner.RED if i % 2 == 0 else Corner.BLUE,
            features={
                "reach_cm_diff": float(i % 7) - 3,
                "win_rate_prior_diff": float(i % 5) - 2,
                "stance_a": "orthodox",
            },
        )


def test_run_training_produz_modelo_e_metricas(db_session: Session) -> None:
    """A pipeline lê o banco, faz o split temporal, treina e mede modelo + baseline."""
    _seed_many(db_session, 20)

    result = run_training(db_session, test_fraction=0.25, random_state=0)

    assert result.n_samples == 20
    assert result.n_train + result.n_test == 20
    assert result.n_test == 5
    # ``stance_a`` (categórica) não entra em X.
    assert "stance_a" not in result.feature_names
    assert "reach_cm_diff" in result.feature_names
    assert 0.0 <= result.model_metrics.accuracy <= 1.0
    assert result.baseline_metrics.roc_auc == 0.5
    assert result.trained_at.tzinfo is not None


def test_save_artifact_persiste_modelo_features_e_metricas(
    db_session: Session, tmp_path: Path
) -> None:
    """O artefato joblib carrega de volta com modelo, features e métricas."""
    _seed_many(db_session, 20)
    result = run_training(db_session, test_fraction=0.25, random_state=0)

    artifact_path = save_artifact(result, directory=tmp_path)

    assert artifact_path.exists()
    payload = joblib.load(artifact_path)
    assert payload["feature_names"] == result.feature_names
    assert payload["n_samples"] == 20
    assert "model" in payload["metrics"]
    assert "baseline" in payload["metrics"]
    # O modelo persistido prediz sobre um X com as mesmas colunas de features.
    amostra = pd.DataFrame([dict.fromkeys(result.feature_names, 0.0)])
    predicoes = payload["model"].predict(amostra)
    assert len(predicoes) == 1
