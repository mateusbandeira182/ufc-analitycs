"""Testes do relatório comparativo do re-treino (fase 2 reaberta -- M5 Slice 06).

``compare_training`` é o helper de reporte testável: a partir de um ``TrainingResult``,
devolve os deltas do modelo vs. o baseline ingênuo **e** vs. o modelo pré-M5
(``PRE_M5_REFERENCE``). A comparação é função pura sobre métricas conhecidas -- não toca o
banco nem treina --, provando a avaliação honesta (sem overclaim) que o ``_log_result``
apenas emite via ``logging``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sklearn.ensemble import HistGradientBoostingClassifier

from analysis.metrics import PRE_M5_REFERENCE, Metrics
from analysis.model import TrainingResult
from analysis.train import compare_training


def _training_result(model: Metrics, baseline: Metrics) -> TrainingResult:
    """Monta um ``TrainingResult`` mínimo com métricas conhecidas (sem treinar)."""
    return TrainingResult(
        model=HistGradientBoostingClassifier(),
        feature_names=["reach_cm_diff"],
        model_metrics=model,
        baseline_metrics=baseline,
        trained_at=datetime.now(UTC),
        n_samples=100,
        n_train=80,
        n_test=20,
        test_fraction=0.2,
    )


def test_compare_training_reporta_vs_baseline_e_vs_pre_m5() -> None:
    """CA-04: o relatório traz o delta vs. baseline E vs. pré-M5, com sinal correto."""
    model = Metrics(accuracy=0.63, log_loss=0.66, roc_auc=0.64, n_samples=20)
    baseline = Metrics(accuracy=0.56, log_loss=0.70, roc_auc=0.50, n_samples=20)
    result = _training_result(model, baseline)

    comparison = compare_training(result)

    # vs. baseline: o modelo supera em accuracy.
    assert comparison.vs_baseline.accuracy == 0.63 - 0.56
    assert comparison.vs_baseline.improves_accuracy is True
    # vs. pré-M5: comparado à referência registrada (capturada no PASSO-00).
    assert comparison.vs_pre_m5.accuracy == 0.63 - PRE_M5_REFERENCE.accuracy
    assert comparison.vs_pre_m5.roc_auc == 0.64 - PRE_M5_REFERENCE.roc_auc


def test_compare_training_sem_ganho_vs_pre_m5_nao_faz_overclaim() -> None:
    """CA-04: quando o modelo não supera o pré-M5, o veredito de accuracy é ``False``."""
    model = Metrics(
        accuracy=PRE_M5_REFERENCE.accuracy - 0.01,
        log_loss=PRE_M5_REFERENCE.log_loss + 0.01,
        roc_auc=PRE_M5_REFERENCE.roc_auc - 0.01,
        n_samples=PRE_M5_REFERENCE.n_samples,
    )
    baseline = Metrics(accuracy=0.56, log_loss=0.70, roc_auc=0.50, n_samples=20)

    comparison = compare_training(_training_result(model, baseline))

    assert comparison.vs_pre_m5.improves_accuracy is False
    assert comparison.vs_pre_m5.accuracy < 0
