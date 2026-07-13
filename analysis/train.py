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
from dataclasses import dataclass
from pathlib import Path

from analysis.metrics import PRE_M5_REFERENCE, Metrics, MetricsDelta, metrics_delta
from analysis.model import DEFAULT_TEST_FRACTION, TrainingResult, run_training, save_artifact
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrainingComparison:
    """Comparação honesta do modelo re-treinado: vs. baseline ingênuo e vs. pré-M5.

    ``vs_pre_m5`` mede o ganho das features novas (M5) contra o bootstrap registrado antes
    da re-materialização (``PRE_M5_REFERENCE``). Ambos os deltas seguem a convenção de
    ``MetricsDelta`` (accuracy/ROC-AUC: maior é melhor; log-loss: menor é melhor).
    """

    vs_baseline: MetricsDelta
    vs_pre_m5: MetricsDelta


def compare_training(
    result: TrainingResult, pre_m5: Metrics = PRE_M5_REFERENCE
) -> TrainingComparison:
    """Deriva os deltas do modelo vs. o baseline e vs. o modelo pré-M5 (função pura)."""
    return TrainingComparison(
        vs_baseline=metrics_delta(result.model_metrics, result.baseline_metrics),
        vs_pre_m5=metrics_delta(result.model_metrics, pre_m5),
    )


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


def _log_delta(rotulo: str, delta: MetricsDelta) -> None:
    """Reporta um bloco de delta (accuracy/log-loss/ROC-AUC) com o veredito de accuracy.

    Sem overclaim: log-loss menor é melhor (delta negativo é melhora); accuracy e ROC-AUC
    maiores são melhores. ``não supera`` é resultado válido -- o valor da slice é a medição.
    """
    veredito = "supera" if delta.improves_accuracy else "não supera"
    logger.info(
        "%s: accuracy=%+.4f log_loss=%+.4f roc_auc=%+.4f -> o modelo %s em accuracy.",
        rotulo,
        delta.accuracy,
        delta.log_loss,
        delta.roc_auc,
        veredito,
    )


def _log_result(result: TrainingResult, artifact_path: Path) -> None:
    """Reporta o resumo do treino: split, modelo vs. baseline, vs. pré-M5 e artefato.

    Deixa explícito se o modelo bate o baseline ingênuo E o modelo pré-M5 (M4) em accuracy
    -- avaliação honesta, sem overclaim (o teto real é a linha de mercado, ~58% do corner
    vermelho). A comparação vs. pré-M5 mede o ganho das features novas (M5).
    """
    logger.info(
        "Treino concluído: %d amostras (%d treino / %d teste, holdout temporal de %.0f%%).",
        result.n_samples,
        result.n_train,
        result.n_test,
        result.test_fraction * 100,
    )
    _log_metrics("Modelo   ", result.model_metrics)
    _log_metrics("Baseline ", result.baseline_metrics)
    _log_metrics("Pré-M5   ", PRE_M5_REFERENCE)
    comparison = compare_training(result)
    _log_delta("Delta vs. baseline", comparison.vs_baseline)
    _log_delta("Delta vs. pré-M5  ", comparison.vs_pre_m5)
    logger.info("Teto real: linha de mercado (~0.58 corner vermelho).")
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
