"""Carregamento do dataset preditivo a partir de ``bout_features`` e split temporal.

Núcleo de dados da fase 2 (modelo preditivo). Lê o cache reconstrutível ``bout_features``
(M4) juntando a data do evento (``bout_features`` -> ``bouts`` -> ``events``), expande o
payload JSONB ``features`` em colunas **numéricas** (X) e mapeia o alvo
``target_winner_corner`` para binário (vermelho=1, azul=0), descartando as lutas de alvo
nulo (NC/empate).

Invariante load-bearing (mesma disciplina anti-leakage do M4): o split é **temporal**,
nunca aleatório. Ordena por data de evento e reserva as lutas mais recentes como holdout
de teste -- nenhuma luta de teste pode ter data anterior a uma luta de treino. Sem isso, o
modelo veria o futuro no treino e as métricas seriam otimistas e inúteis em produção.

Fronteira dinâmica tipada: o DataFrame do Pandas é borda dinâmica (``pandas.*`` tem
``follow_imports=skip`` no ``pyproject.toml``); a leitura converte ``bout_id`` para ``int``
e o alvo para ``str``/``None`` na borda, e as colunas categóricas (``stance_*``) ficam fora
de X -- o classificador consome apenas numérico, com ``NaN`` explícito preservado (o
``HistGradientBoostingClassifier`` trata ausência nativamente, sem imputação especulativa).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.models import Bout
from apps.events.models import Event
from apps.features.models import BoutFeatures

COL_BOUT_ID = "bout_id"
COL_EVENT_DATE = "event_date"
COL_TARGET = "target_winner_corner"
COL_FEATURES = "features"

# Alvo binário: canto vermelho = 1 (o baseline ingênuo prevê sempre 1), azul = 0.
_CORNER_TO_LABEL: dict[str, int] = {"red": 1, "blue": 0}


@dataclass(frozen=True)
class Dataset:
    """Dataset preditivo pronto para o split temporal.

    ``features`` (X) só contém colunas numéricas; ``target`` (y) é binário (0/1);
    ``event_date`` e ``bout_id`` acompanham cada linha (alinhados por índice) para
    ordenar o split temporal de forma determinística.
    """

    features: pd.DataFrame
    target: pd.Series
    event_date: pd.Series
    bout_id: pd.Series
    feature_names: list[str]


@dataclass(frozen=True)
class TemporalSplit:
    """Resultado do split temporal treino/teste, com a fronteira de datas explícita.

    ``boundary_date`` é a data máxima do treino; por construção, ``event_test.min()`` é
    maior ou igual a ela -- a prova de ausência de vazamento temporal.
    """

    x_train: pd.DataFrame
    x_test: pd.DataFrame
    y_train: pd.Series
    y_test: pd.Series
    event_train: pd.Series
    event_test: pd.Series
    boundary_date: date


def read_bout_features(session: Session) -> pd.DataFrame:
    """Lê ``bout_features`` juntando a data do evento; devolve a frame crua.

    Junta ``bout_features`` -> ``bouts`` -> ``events`` para obter a data por luta. Cada
    linha carrega ``bout_id`` (int), ``event_date`` (data de calendário), o alvo como
    ``str``/``None`` (``"red"``/``"blue"``) e ``features`` como ``dict`` -- a expansão em
    colunas fica para ``build_dataset``.
    """
    stmt = (
        select(
            BoutFeatures.bout_id.label(COL_BOUT_ID),
            Event.date.label(COL_EVENT_DATE),
            BoutFeatures.target_winner_corner.label(COL_TARGET),
            BoutFeatures.features.label(COL_FEATURES),
        )
        .join(Bout, Bout.id == BoutFeatures.bout_id)
        .join(Event, Event.id == Bout.event_id)
    )
    records: list[dict[str, object]] = [
        {
            COL_BOUT_ID: int(row.bout_id),
            COL_EVENT_DATE: row.event_date,
            COL_TARGET: None if row.target_winner_corner is None else str(row.target_winner_corner),
            COL_FEATURES: dict(row.features),
        }
        for row in session.execute(stmt).all()
    ]
    return pd.DataFrame.from_records(
        records,
        columns=[COL_BOUT_ID, COL_EVENT_DATE, COL_TARGET, COL_FEATURES],
    )


def _label_for(value: object) -> int:
    """Mapeia o alvo ``"red"``/``"blue"`` para 1/0; um valor inesperado falha visível."""
    label = _CORNER_TO_LABEL.get(str(value))
    if label is None:
        aceitos = ", ".join(sorted(_CORNER_TO_LABEL))
        raise ValueError(f"Alvo winner_corner inesperado: {value!r}; valores aceitos: {aceitos}")
    return label


def _numeric_feature_columns(expanded: pd.DataFrame) -> list[str]:
    """Colunas de feature numéricas: exclui as que carregam qualquer valor string.

    As categóricas do M4 (``stance_a``/``stance_b``) guardam strings (``"orthodox"``...)
    e ficam fora de X -- o classificador consome apenas numérico. Uma coluna toda nula
    (sem string) é mantida como ``NaN`` numérico (sem informação, mas inofensiva).
    """
    numeric: list[str] = []
    for column in expanded.columns:
        has_string = bool(expanded[column].map(lambda value: isinstance(value, str)).any())
        if not has_string:
            numeric.append(str(column))
    return numeric


def build_dataset(raw: pd.DataFrame) -> Dataset:
    """Constrói o dataset preditivo a partir da frame crua de ``read_bout_features``.

    Descarta linhas de alvo nulo (NC/empate), expande o JSONB ``features`` em colunas,
    seleciona apenas as numéricas (X) e mapeia o alvo para binário (y). O ``NaN`` das
    features é preservado (ausência explícita, sem imputação).
    """
    decided = raw[raw[COL_TARGET].notna()].reset_index(drop=True)
    expanded = pd.DataFrame(list(decided[COL_FEATURES]), index=decided.index)
    numeric_columns = _numeric_feature_columns(expanded)
    if numeric_columns:
        features = expanded[numeric_columns].apply(pd.to_numeric).astype("float64")
    else:
        features = pd.DataFrame(index=decided.index)
    target = decided[COL_TARGET].map(_label_for).astype("int64")
    return Dataset(
        features=features,
        target=target,
        event_date=decided[COL_EVENT_DATE],
        bout_id=decided[COL_BOUT_ID],
        feature_names=numeric_columns,
    )


def temporal_split(dataset: Dataset, test_fraction: float = 0.2) -> TemporalSplit:
    """Separa treino/teste por data de evento: as lutas mais recentes viram o teste.

    Ordena por ``(event_date, bout_id)`` de forma estável e reserva a fração final como
    holdout. Garante ``event_test.min() >= event_train.max()`` -- prova de ausência de
    vazamento temporal. ``test_fraction`` deve estar em ``(0, 1)`` e sobrar ao menos uma
    luta para treino e uma para teste.
    """
    if not 0.0 < test_fraction < 1.0:
        raise ValueError(f"test_fraction deve estar em (0, 1); recebido: {test_fraction}")
    order = (
        pd.DataFrame(
            {
                COL_EVENT_DATE: dataset.event_date.to_numpy(),
                COL_BOUT_ID: dataset.bout_id.to_numpy(),
            }
        )
        .sort_values([COL_EVENT_DATE, COL_BOUT_ID], kind="stable")
        .index.to_numpy()
    )
    n_total = len(order)
    n_test = max(1, round(n_total * test_fraction))
    n_train = n_total - n_test
    if n_train <= 0:
        raise ValueError(
            f"Amostras insuficientes ({n_total}) para um split temporal com treino não vazio."
        )
    train_pos = order[:n_train]
    test_pos = order[n_train:]
    event_train = dataset.event_date.iloc[train_pos]
    return TemporalSplit(
        x_train=dataset.features.iloc[train_pos],
        x_test=dataset.features.iloc[test_pos],
        y_train=dataset.target.iloc[train_pos],
        y_test=dataset.target.iloc[test_pos],
        event_train=event_train,
        event_test=dataset.event_date.iloc[test_pos],
        boundary_date=event_train.max(),
    )
