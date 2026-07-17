"""Treino, avaliação e persistência do modelo preditivo de resultado de luta (fase 2).

Reúne a pipeline: lê ``bout_features`` -> ``build_dataset`` -> split **temporal** ->
treina um ``HistGradientBoostingClassifier`` (trata ``NaN`` nativamente, casando com a
ausência explícita do M4) -> avalia modelo e baseline no holdout temporal -> empacota tudo
num ``TrainingResult`` -> persiste um artefato joblib reexecutável.

Determinismo: ``random_state`` fixo torna o treino reprodutível (mesmas predições). A
pipeline é reexecutável conforme o banco cresce ("ir treinando") -- cada execução relê o
estado atual de ``bout_features`` e regenera o artefato.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sqlalchemy.orm import Session

from analysis.dataset import (
    Dataset,
    TemporalSplit,
    build_dataset,
    read_bout_features,
    temporal_split,
)
from analysis.metrics import Metrics, baseline_metrics, compute_metrics

logger = logging.getLogger(__name__)

RANDOM_STATE = 0
DEFAULT_TEST_FRACTION = 0.2

# Artefato binário sob o pacote; ignorado pelo git (ver .gitignore).
ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"
ARTIFACT_NAME = "fight_winner_model.joblib"


@dataclass(frozen=True)
class LoadedModel:
    """Modelo recarregado do artefato, pronto para servir a predição.

    Simetria com ``save_artifact``/``TrainingResult``: expõe o classificador treinado e a
    lista ordenada de features (``feature_names``) que ele consome -- a mesma ordem/colunas
    do treino, contrato de alinhamento do vetor de features no momento da predição.
    """

    model: HistGradientBoostingClassifier
    feature_names: list[str]


@dataclass(frozen=True)
class TrainingResult:
    """Saída coesa de uma execução de treino: modelo, features, métricas e metadados."""

    model: HistGradientBoostingClassifier
    feature_names: list[str]
    model_metrics: Metrics
    baseline_metrics: Metrics
    trained_at: datetime
    n_samples: int
    n_train: int
    n_test: int
    test_fraction: float


def train_model(
    x_train: pd.DataFrame,
    y_train: pd.Series,
    random_state: int = RANDOM_STATE,
) -> HistGradientBoostingClassifier:
    """Treina o classificador com ``random_state`` fixo (treino determinístico).

    ``HistGradientBoostingClassifier`` trata ``NaN`` nativamente -- casa com a ausência
    explícita das features do M4, sem imputação especulativa.
    """
    model = HistGradientBoostingClassifier(random_state=random_state)
    model.fit(x_train, y_train)
    return model


def _evaluate_model(model: HistGradientBoostingClassifier, split: TemporalSplit) -> Metrics:
    """Avalia o modelo no holdout temporal usando a probabilidade da classe positiva."""
    y_pred = model.predict(split.x_test)
    y_prob = model.predict_proba(split.x_test)[:, 1]
    return compute_metrics(split.y_test, y_pred, y_prob)


def run_training(
    session: Session,
    test_fraction: float = DEFAULT_TEST_FRACTION,
    random_state: int = RANDOM_STATE,
) -> TrainingResult:
    """Roda a pipeline completa sobre o estado atual de ``bout_features`` (não commita).

    Lê o dataset, faz o split temporal, treina o modelo e avalia modelo e baseline no
    holdout. É read-only (código de análise sobre o granular derivado); o ``main`` de
    ``analysis.train`` abre a sessão. Levanta ``ValueError`` se não há linhas com alvo.
    """
    dataset: Dataset = build_dataset(read_bout_features(session))
    n_samples = len(dataset.target)
    if n_samples == 0:
        raise ValueError("bout_features não tem linhas com alvo definido; nada a treinar.")
    split = temporal_split(dataset, test_fraction)
    model = train_model(split.x_train, split.y_train, random_state)
    return TrainingResult(
        model=model,
        feature_names=dataset.feature_names,
        model_metrics=_evaluate_model(model, split),
        baseline_metrics=baseline_metrics(split.y_train, split.y_test),
        trained_at=datetime.now(UTC),
        n_samples=n_samples,
        n_train=len(split.y_train),
        n_test=len(split.y_test),
        test_fraction=test_fraction,
    )


def save_artifact(result: TrainingResult, directory: Path = ARTIFACTS_DIR) -> Path:
    """Persiste o modelo, a lista de features, as métricas e os metadados num joblib.

    O artefato é reexecutável: recarregá-lo devolve o modelo pronto para predição e os
    metadados de rastreio (``trained_at``, ``n_samples``). Cria o diretório se preciso.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ARTIFACT_NAME
    payload: dict[str, object] = {
        "model": result.model,
        "feature_names": result.feature_names,
        "metrics": {
            "model": asdict(result.model_metrics),
            "baseline": asdict(result.baseline_metrics),
        },
        "trained_at": result.trained_at.isoformat(),
        "n_samples": result.n_samples,
        "n_train": result.n_train,
        "n_test": result.n_test,
        "test_fraction": result.test_fraction,
    }
    joblib.dump(payload, path)
    return path


def load_artifact(directory: Path = ARTIFACTS_DIR) -> LoadedModel:
    """Recarrega o modelo e a lista de features do artefato joblib persistido.

    Simetria de ``save_artifact``: devolve o classificador pronto para predição e as
    ``feature_names`` na ordem do treino. O joblib é fronteira dinâmica (``joblib.load``
    devolve ``Any``): o tipo é estreitado na borda -- o dicionário e o modelo são validados
    por ``isinstance`` e as features convertidas para ``list[str]``, sem propagar ``Any``.
    Levanta ``FileNotFoundError`` se o artefato não existe (nunca devolve modelo vazio).
    """
    path = directory / ARTIFACT_NAME
    if not path.exists():
        raise FileNotFoundError(
            f"Artefato do modelo não encontrado em {path}; treine com 'python -m analysis.train'."
        )
    raw = joblib.load(path)
    if not isinstance(raw, dict):
        raise TypeError(f"Artefato inesperado em {path}: esperado dict, obtido {type(raw)!r}.")
    model = raw["model"]
    if not isinstance(model, HistGradientBoostingClassifier):
        raise TypeError(
            f"Modelo do artefato com tipo inesperado: {type(model)!r} "
            f"(esperado HistGradientBoostingClassifier)."
        )
    feature_names = [str(name) for name in raw["feature_names"]]
    return LoadedModel(model=model, feature_names=feature_names)
