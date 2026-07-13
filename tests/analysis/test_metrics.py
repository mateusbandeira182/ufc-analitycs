"""Testes das métricas de avaliação honesta e do baseline ingênuo (fase 2).

``compute_metrics`` reporta accuracy, log-loss e ROC-AUC sobre probabilidades;
``baseline_metrics`` mede o preditor ingênuo que **prevê sempre o corner vermelho**
(rótulo 1), com probabilidade constante igual à prevalência de vermelho no treino.
Ambos são funções puras, exercitadas sobre conjuntos conhecidos.
"""

from __future__ import annotations

import math

import pandas as pd

from analysis.metrics import baseline_metrics, compute_metrics


def test_compute_metrics_preditor_perfeito() -> None:
    """Um preditor perfeito tem accuracy e ROC-AUC 1.0 e log-loss baixo."""
    y_true = [1, 0, 1, 0]
    y_pred = [1, 0, 1, 0]
    y_prob = [0.9, 0.1, 0.85, 0.2]

    metrics = compute_metrics(y_true, y_pred, y_prob)

    assert metrics.accuracy == 1.0
    assert metrics.roc_auc == 1.0
    assert metrics.log_loss < 0.3
    assert metrics.n_samples == 4


def test_baseline_preve_sempre_corner_vermelho() -> None:
    """O baseline prevê sempre vermelho (1): accuracy = prevalência de vermelho no teste."""
    y_train = pd.Series([1, 1, 0, 1])  # prevalência de vermelho no treino = 0.75
    y_test = pd.Series([1, 1, 0, 0])  # metade vermelho

    metrics = baseline_metrics(y_train, y_test)

    assert metrics.accuracy == 0.5
    # Preditor constante: ROC-AUC degenera para 0.5 (nenhum poder de ordenação).
    assert metrics.roc_auc == 0.5
    assert metrics.n_samples == 4


def test_baseline_usa_prevalencia_do_treino_como_probabilidade() -> None:
    """A probabilidade constante do baseline vem da prevalência de vermelho no treino."""
    y_train = pd.Series([1, 1, 1, 0])  # prevalência 0.75
    y_test = pd.Series([1, 0])

    metrics = baseline_metrics(y_train, y_test)

    # log-loss de p constante 0.75 contra [1, 0]: -(ln 0.75 + ln 0.25) / 2.
    esperado = -(math.log(0.75) + math.log(0.25)) / 2
    assert metrics.accuracy == 0.5
    assert abs(metrics.log_loss - esperado) < 1e-9
