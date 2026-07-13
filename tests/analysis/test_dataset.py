"""Testes do carregamento e do split temporal do dataset preditivo (fase 2).

Cobrem duas responsabilidades do pacote ``analysis``:

- ``build_dataset``: expande o JSONB ``features`` de ``bout_features`` em colunas
  numéricas (X), mapeia o alvo ``target_winner_corner`` para binário (vermelho=1,
  azul=0) e descarta as linhas de alvo nulo (NC/empate). Colunas categóricas
  (``stance_*``) ficam fora de X -- o modelo consome apenas numérico.
- ``temporal_split``: separa treino/teste **por data de evento** (nunca aleatório).
  O teste mais importante é o de ausência de vazamento temporal: toda luta de teste
  tem data maior ou igual à data máxima do treino.

``read_bout_features`` (leitura do banco) é exercitada contra o Postgres de teste
transacional; a expansão e o split são funções puras sobre DataFrame sintético.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from analysis.dataset import (
    build_dataset,
    read_bout_features,
    temporal_split,
)
from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout
from apps.events.models import Event
from apps.features.models import BoutFeatures


def _raw_row(
    *,
    bout_id: int,
    event_date: date,
    target: str | None,
    features: dict[str, object],
) -> dict[str, object]:
    """Monta uma linha crua no formato devolvido por ``read_bout_features``."""
    return {
        "bout_id": bout_id,
        "event_date": event_date,
        "target_winner_corner": target,
        "features": features,
    }


def test_build_dataset_expande_features_e_mapeia_alvo_binario() -> None:
    """Expande o JSONB em colunas numéricas e mapeia vermelho=1/azul=0."""
    raw = pd.DataFrame(
        [
            _raw_row(
                bout_id=1,
                event_date=date(2020, 1, 1),
                target="red",
                features={"reach_cm_diff": 5.0, "stance_a": "orthodox"},
            ),
            _raw_row(
                bout_id=2,
                event_date=date(2020, 2, 1),
                target="blue",
                features={"reach_cm_diff": -3.0, "stance_a": None},
            ),
        ]
    )

    dataset = build_dataset(raw)

    # ``stance_a`` (categórica) fica fora de X; só a coluna numérica entra.
    assert dataset.feature_names == ["reach_cm_diff"]
    assert list(dataset.features.columns) == ["reach_cm_diff"]
    assert dataset.target.tolist() == [1, 0]


def test_build_dataset_descarta_linhas_com_alvo_nulo() -> None:
    """Linhas de alvo nulo (NC/empate) não entram no treino."""
    raw = pd.DataFrame(
        [
            _raw_row(
                bout_id=1,
                event_date=date(2020, 1, 1),
                target="red",
                features={"reach_cm_diff": 1.0},
            ),
            _raw_row(
                bout_id=2,
                event_date=date(2020, 2, 1),
                target=None,
                features={"reach_cm_diff": 2.0},
            ),
        ]
    )

    dataset = build_dataset(raw)

    assert len(dataset.target) == 1
    assert dataset.bout_id.tolist() == [1]


def test_build_dataset_preserva_nan_das_features() -> None:
    """Ausência explícita (``None``) vira ``NaN`` numérico -- o modelo trata nativamente."""
    raw = pd.DataFrame(
        [
            _raw_row(
                bout_id=1,
                event_date=date(2020, 1, 1),
                target="red",
                features={"reach_cm_diff": None},
            ),
        ]
    )

    dataset = build_dataset(raw)

    assert bool(dataset.features["reach_cm_diff"].isna().iloc[0])


def _dataset_from_dates(dates: list[date]) -> pd.DataFrame:
    """Frame crua com uma coluna numérica trivial, alvos alternados e datas dadas."""
    return pd.DataFrame(
        [
            _raw_row(
                bout_id=index,
                event_date=event_date,
                target="red" if index % 2 == 0 else "blue",
                features={"reach_cm_diff": float(index)},
            )
            for index, event_date in enumerate(dates)
        ]
    )


def test_temporal_split_nao_vaza_futuro_para_o_treino() -> None:
    """Invariante load-bearing: nenhuma luta de teste tem data anterior a uma de treino."""
    # Datas propositalmente fora de ordem na frame de entrada.
    dates = [
        date(2021, 5, 1),
        date(2019, 1, 1),
        date(2020, 3, 1),
        date(2022, 7, 1),
        date(2018, 2, 1),
        date(2023, 9, 1),
        date(2017, 4, 1),
        date(2024, 6, 1),
        date(2016, 8, 1),
        date(2025, 2, 1),
    ]
    dataset = build_dataset(_dataset_from_dates(dates))

    split = temporal_split(dataset, test_fraction=0.3)

    assert split.event_test.min() >= split.event_train.max()
    assert split.boundary_date == split.event_train.max()


def test_temporal_split_respeita_a_fracao_de_teste() -> None:
    """A fração de teste define o tamanho do holdout mais recente (arredondado)."""
    dates = [date(2020, 1, 1) + timedelta(days=30 * i) for i in range(10)]
    dataset = build_dataset(_dataset_from_dates(dates))

    split = temporal_split(dataset, test_fraction=0.2)

    assert len(split.y_test) == 2
    assert len(split.y_train) == 8
    assert len(split.y_train) + len(split.y_test) == len(dataset.target)


def _seed_bout_features(
    session: Session,
    *,
    event_date: date,
    target: Corner | None,
    features: dict[str, object],
) -> int:
    """Semeia evento + luta + ``bout_features`` e devolve o ``bout_id``.

    O granular completo (``bout_fighters``) não é necessário para a leitura do
    dataset, que junta ``bout_features`` -> ``bouts`` -> ``events`` pela data.
    """
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
    return int(bout.id)


def test_read_bout_features_junta_data_do_evento_e_expande(db_session: Session) -> None:
    """Leitura real: junta a data do evento e devolve ``features`` como dict + alvo string."""
    bout_id = _seed_bout_features(
        db_session,
        event_date=date(2021, 6, 15),
        target=Corner.RED,
        features={"reach_cm_diff": 4.0, "stance_a": "southpaw"},
    )

    raw = read_bout_features(db_session)

    assert list(raw.columns) == ["bout_id", "event_date", "target_winner_corner", "features"]
    linha = raw[raw["bout_id"] == bout_id].iloc[0]
    assert linha["event_date"] == date(2021, 6, 15)
    assert linha["target_winner_corner"] == "red"
    assert linha["features"] == {"reach_cm_diff": 4.0, "stance_a": "southpaw"}


def test_read_bout_features_alimenta_build_dataset(db_session: Session) -> None:
    """A leitura real encadeia em ``build_dataset``: alvo binário e X só numérico."""
    _seed_bout_features(
        db_session,
        event_date=date(2021, 6, 15),
        target=Corner.BLUE,
        features={"reach_cm_diff": 4.0, "stance_a": "southpaw"},
    )

    dataset = build_dataset(read_bout_features(db_session))

    assert dataset.feature_names == ["reach_cm_diff"]
    assert dataset.target.tolist() == [0]
