"""Métricas de avaliação honesta do modelo preditivo e do baseline ingênuo (fase 2).

Avaliação honesta (framing da SPEC 005): sobre o conjunto de teste **temporal**, reporta
accuracy, log-loss e ROC-AUC. O baseline ingênuo prevê **sempre o corner vermelho**
(rótulo 1) com probabilidade constante igual à prevalência de vermelho no treino -- é o
piso que o modelo precisa bater para ter qualquer valor. O teto real é a linha de mercado
(~58% de acerto do corner vermelho); as odds já incorporam quase toda a informação
tabular, então não há overclaim a fazer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score

# Rótulos binários fixos: garante que ``log_loss`` conheça as duas classes mesmo quando
# as predições são constantes (caso do baseline), evitando erro de rótulo ausente.
_BINARY_LABELS = [0, 1]


@dataclass(frozen=True)
class Metrics:
    """Métricas de classificação binária sobre um conjunto de avaliação."""

    accuracy: float
    log_loss: float
    roc_auc: float
    n_samples: int


@dataclass(frozen=True)
class MetricsDelta:
    """Diferença ``current - reference`` de cada métrica, para comparação honesta.

    Convenção de sinal: accuracy e ROC-AUC seguem "maior é melhor" (ganho -> positivo);
    log-loss segue "menor é melhor" (melhora -> negativo). ``improves_accuracy`` resume o
    veredito de accuracy sem overclaim (empate/regressão -> ``False``).
    """

    accuracy: float
    log_loss: float
    roc_auc: float

    @property
    def improves_accuracy(self) -> bool:
        """``True`` só quando a accuracy corrente supera estritamente a de referência."""
        return self.accuracy > 0.0


# Referência do bootstrap pré-M5 (features do M4), holdout temporal de 20%. Capturada no
# PASSO-00 do Plano 006-06 rodando ``python -m analysis.train`` ANTES de estender as
# features (banco ufc_databum seedado, 8170 amostras; 1634 no teste). Registrada em código
# porque o modelo pré-M5 não é re-treinável após a re-materialização (as features antigas
# deixam de existir): é a única representação fiel do baseline a superar.
PRE_M5_REFERENCE = Metrics(
    accuracy=0.6009791921664627,
    log_loss=0.6860892556792704,
    roc_auc=0.6180559786979942,
    n_samples=1634,
)


def metrics_delta(current: Metrics, reference: Metrics) -> MetricsDelta:
    """Delta ``current - reference`` de accuracy, log-loss e ROC-AUC (comparação testável)."""
    return MetricsDelta(
        accuracy=current.accuracy - reference.accuracy,
        log_loss=current.log_loss - reference.log_loss,
        roc_auc=current.roc_auc - reference.roc_auc,
    )


def compute_metrics(
    y_true: pd.Series | Sequence[int],
    y_pred: pd.Series | Sequence[int],
    y_prob: pd.Series | Sequence[float],
) -> Metrics:
    """Calcula accuracy, log-loss e ROC-AUC a partir de rótulos verdadeiros e probabilidades.

    ``y_prob`` é a probabilidade da classe positiva (corner vermelho, rótulo 1). O ROC-AUC
    de um preditor constante degenera para 0.5 (nenhum poder de ordenação) -- comportamento
    honesto, sem mascarar o baseline.
    """
    return Metrics(
        accuracy=float(accuracy_score(y_true, y_pred)),
        log_loss=float(log_loss(y_true, y_prob, labels=_BINARY_LABELS)),
        roc_auc=float(roc_auc_score(y_true, y_prob)),
        n_samples=len(y_true),
    )


def baseline_metrics(
    y_train: pd.Series | Sequence[int],
    y_test: pd.Series | Sequence[int],
) -> Metrics:
    """Métricas do baseline ingênuo que prevê sempre o corner vermelho (rótulo 1).

    A probabilidade constante é a prevalência de vermelho no treino; a predição é sempre 1.
    A accuracy resultante é a prevalência de vermelho no teste -- o piso a ser superado.
    """
    red_rate = float(pd.Series(list(y_train)).mean())
    n_test = len(list(y_test))
    y_pred = [1] * n_test
    y_prob = [red_rate] * n_test
    return compute_metrics(y_test, y_pred, y_prob)
