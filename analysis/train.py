"""CLI de (re)treino do modelo preditivo: ``python -m analysis.train``.

Reexecutável: cada execução relê o estado atual de ``bout_features``, treina, avalia
(modelo vs. baseline) no holdout temporal, reporta as métricas via ``logging`` (``print``
é proibido, regra ``T20``) e salva o artefato joblib. ``main`` é fino -- abre a ``Session``
real e delega a ``run_training``/``save_artifact`` de ``analysis.model``; a lógica testável
vive lá.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from analysis.metrics import Metrics
from analysis.model import DEFAULT_TEST_FRACTION, TrainingResult, run_training, save_artifact
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)


def _log_metrics(rotulo: str, metrics: Metrics) -> None:
    """Reporta um bloco de métricas (modelo ou baseline) via ``logging``."""
    logger.info(
        "%s: accuracy=%.4f log_loss=%.4f roc_auc=%.4f (n=%d)",
        rotulo,
        metrics.accuracy,
        metrics.log_loss,
        metrics.roc_auc,
        metrics.n_samples,
    )


def _log_result(result: TrainingResult, artifact_path: Path) -> None:
    """Reporta o resumo do treino: tamanhos do split, modelo vs. baseline e artefato.

    Deixa explícito se o modelo bate o baseline em accuracy -- avaliação honesta, sem
    overclaim (o teto real é a linha de mercado, ~58% do corner vermelho).
    """
    logger.info(
        "Treino concluído: %d amostras (%d treino / %d teste, holdout temporal de %.0f%%).",
        result.n_samples,
        result.n_train,
        result.n_test,
        result.test_fraction * 100,
    )
    _log_metrics("Modelo  ", result.model_metrics)
    _log_metrics("Baseline", result.baseline_metrics)
    delta = result.model_metrics.accuracy - result.baseline_metrics.accuracy
    veredito = "supera" if delta > 0 else "não supera"
    logger.info(
        "O modelo %s o baseline ingênuo em accuracy (delta=%+.4f). Teto real: linha de "
        "mercado (~0.58 corner vermelho).",
        veredito,
        delta,
    )
    logger.info("Artefato salvo em: %s", artifact_path)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de ``python -m analysis.train``."""
    parser = argparse.ArgumentParser(
        description=(
            "Treina e persiste o modelo preditivo de resultado de luta sobre bout_features "
            "(split temporal, avaliação honesta vs. baseline)."
        ),
    )
    parser.add_argument(
        "--test-fraction",
        type=float,
        default=DEFAULT_TEST_FRACTION,
        help="Fração das lutas mais recentes reservada como holdout de teste temporal.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint: abre a ``Session`` real, treina, reporta as métricas e salva o artefato."""
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    with SessionLocal() as session:
        result = run_training(session, test_fraction=args.test_fraction)
    artifact_path = save_artifact(result)
    _log_result(result, artifact_path)


if __name__ == "__main__":
    main()
