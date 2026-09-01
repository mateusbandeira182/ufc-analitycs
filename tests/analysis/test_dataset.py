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

import logging
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from analysis.dataset import (
    FIRST_RELIABLE_CORNER_DATE,
    build_dataset,
    read_bout_features,
    restrict_to_trainable_columns,
    temporal_split,
)
from analysis.model import train_model
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
    """Ausência explícita (``None``) vira ``NaN`` numérico -- o modelo trata nativamente.

    A coluna tem ao menos um valor real (não é 100% NaN), logo é preservada: o ``NaN`` da
    outra linha permanece como ausência explícita, sem imputação.
    """
    raw = pd.DataFrame(
        [
            _raw_row(
                bout_id=1,
                event_date=date(2020, 1, 1),
                target="red",
                features={"reach_cm_diff": None},
            ),
            _raw_row(
                bout_id=2,
                event_date=date(2020, 2, 1),
                target="blue",
                features={"reach_cm_diff": 4.0},
            ),
        ]
    )

    dataset = build_dataset(raw)

    assert bool(dataset.features["reach_cm_diff"].isna().iloc[0])
    assert dataset.features["reach_cm_diff"].iloc[1] == 4.0


def test_build_dataset_descarta_coluna_de_feature_toda_nan() -> None:
    """Coluna de feature 100% NaN é descartada antes do treino (backfill parcial legítimo).

    Cenário realista de rollout desenhado pela própria SPEC: os splits (Sprint 02, 0 quota, sem
    gate) podem ser materializados antes do round-a-round (Sprint 05, atrás de gate humano). Nesse
    intervalo, as colunas de dinâmica por round ficam 100% NaN e o
    ``HistGradientBoostingClassifier`` levanta ``ValueError`` no ``fit``. A guarda remove só a
    coluna toda-nula; as demais (com dado) permanecem e o treino roda.
    """
    raw = pd.DataFrame(
        [
            _raw_row(
                bout_id=index,
                event_date=date(2020, 1, 1) + timedelta(days=30 * index),
                target="red" if index % 2 == 0 else "blue",
                features={
                    "reach_cm_diff": float(index % 7) - 3,
                    # Round-a-round ainda não backfillado: coluna inteira sem valor.
                    "round1_sig_strike_share_r3_diff": None,
                },
            )
            for index in range(20)
        ]
    )

    dataset = build_dataset(raw)

    assert "round1_sig_strike_share_r3_diff" not in dataset.feature_names
    assert "round1_sig_strike_share_r3_diff" not in dataset.features.columns
    assert "reach_cm_diff" in dataset.feature_names
    # Sem a guarda, este ``fit`` quebra na coluna 100% NaN; com ela, o treino roda ponta a ponta.
    model = train_model(dataset.features, dataset.target, random_state=0)
    assert len(model.predict(dataset.features)) == 20


def test_build_dataset_preserva_coluna_parcialmente_nan() -> None:
    """Coluna com ao menos um valor (parcialmente NaN) é preservada intacta.

    O ``HistGradientBoostingClassifier`` trata NaN nativamente -- só a coluna 100% NaN quebra o
    treino. A guarda não pode remover colunas parcialmente preenchidas: fazê-lo descartaria
    informação real e mascararia o comportamento normal (degradação legítima, não silenciador).
    """
    raw = pd.DataFrame(
        [
            _raw_row(
                bout_id=1,
                event_date=date(2020, 1, 1),
                target="red",
                features={"reach_cm_diff": 5.0, "share_head_r3_diff": None},
            ),
            _raw_row(
                bout_id=2,
                event_date=date(2020, 2, 1),
                target="blue",
                features={"reach_cm_diff": -3.0, "share_head_r3_diff": 0.42},
            ),
        ]
    )

    dataset = build_dataset(raw)

    assert "share_head_r3_diff" in dataset.feature_names
    assert bool(dataset.features["share_head_r3_diff"].isna().iloc[0])
    assert dataset.features["share_head_r3_diff"].iloc[1] == 0.42


def test_build_dataset_descarta_lutas_com_canto_fabricado_anteriores_ao_corte() -> None:
    """Luta anterior a ``FIRST_RELIABLE_CORNER_DATE`` sai do dataset; 2010+ permanece.

    Nos eventos do Kaggle anteriores a 2010 o canto foi preenchido com a ordem do resultado
    (o vermelho venceu 100,0% das lutas em **todos** os anos de 1994 a 2009). Como o alvo do
    modelo é justamente o canto, ali o rótulo é o vencedor renomeado -- treinar com ele é
    treinar com rótulo que sabemos ser falso. O corte é seco, não gradiente: a luta do
    próprio dia do corte já entra.
    """
    raw = pd.DataFrame(
        [
            _raw_row(
                bout_id=1,
                event_date=date(2009, 12, 31),
                target="red",
                features={"reach_cm_diff": 1.0},
            ),
            _raw_row(
                bout_id=2,
                event_date=FIRST_RELIABLE_CORNER_DATE,
                target="blue",
                features={"reach_cm_diff": 2.0},
            ),
            _raw_row(
                bout_id=3,
                event_date=date(2015, 6, 1),
                target="red",
                features={"reach_cm_diff": 3.0},
            ),
        ]
    )

    dataset = build_dataset(raw)

    assert dataset.bout_id.tolist() == [2, 3]
    assert dataset.target.tolist() == [0, 1]
    assert dataset.features["reach_cm_diff"].tolist() == [2.0, 3.0]


def test_build_dataset_loga_quantas_lutas_de_canto_fabricado_descartou(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """O descarte é explícito no log: quantas lutas saíram, a partir de quando e por quê.

    Mesma disciplina da guarda de features toda-NaN: um filtro silencioso recria o problema
    que ela existe para resolver -- ninguém descobre que o dataset encolheu nem por qual
    motivo.
    """
    raw = pd.DataFrame(
        [
            _raw_row(
                bout_id=1,
                event_date=date(2005, 3, 1),
                target="red",
                features={"reach_cm_diff": 1.0},
            ),
            _raw_row(
                bout_id=2,
                event_date=date(2009, 8, 1),
                target="red",
                features={"reach_cm_diff": 2.0},
            ),
            _raw_row(
                bout_id=3,
                event_date=date(2018, 4, 1),
                target="blue",
                features={"reach_cm_diff": 3.0},
            ),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="analysis.dataset"):
        build_dataset(raw)

    mensagem = caplog.text
    assert "2 luta(s)" in mensagem
    assert FIRST_RELIABLE_CORNER_DATE.isoformat() in mensagem
    assert "canto" in mensagem


def test_build_dataset_nao_loga_descarte_quando_todas_as_lutas_sao_do_periodo_confiavel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dataset inteiro em 2010+: nada é descartado e o log fica limpo.

    Impede que o aviso vire ruído de rotina -- ele só aparece quando há de fato luta com
    alvo fabricado a descartar.
    """
    raw = _dataset_from_dates([date(2019, 1, 1), date(2020, 2, 1), date(2021, 3, 1)])

    with caplog.at_level(logging.WARNING, logger="analysis.dataset"):
        dataset = build_dataset(raw)

    assert len(dataset.target) == 3
    assert caplog.text == ""


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


def _raw_com_feature_tardia(n: int, n_preenchidas: int) -> pd.DataFrame:
    """Frame crua de ``n`` lutas em datas crescentes; uma feature só nas ``n_preenchidas`` finais.

    Reproduz a assimetria do piloto da Sprint 007-05: a coluna de round-a-round tem dado real,
    mas apenas nas lutas mais recentes -- exatamente as que o split temporal reserva para o
    holdout.
    """
    return pd.DataFrame(
        [
            _raw_row(
                bout_id=index,
                event_date=date(2020, 1, 1) + timedelta(days=30 * index),
                target="red" if index % 2 == 0 else "blue",
                features={
                    "reach_cm_diff": float(index % 7) - 3,
                    "round1_sig_strike_share_r3_diff": (
                        0.5 if index >= n - n_preenchidas else None
                    ),
                },
            )
            for index in range(n)
        ]
    )


def test_restrict_to_trainable_columns_descarta_feature_preenchida_so_depois_do_corte() -> None:
    """Feature com dado apenas do lado do teste é invisível ao treino e sai das duas fatias.

    Cenário real observado contra o banco de desenvolvimento: o piloto de round-a-round cobre
    apenas as lutas mais recentes, então a coluna tem dado no dataset inteiro (a guarda global de
    ``build_dataset`` a preserva) mas é 100% ``NaN`` **dentro da fatia de treino** -- e o
    ``HistGradientBoostingClassifier`` levanta ``ValueError`` no binning. O descarte tem de sair
    das duas fatias: um modelo treinado sem a coluna não pode recebê-la na predição.
    """
    dataset = build_dataset(_raw_com_feature_tardia(20, n_preenchidas=5))
    # A guarda global não pega o caso: a coluna tem dado, só que todo do lado do teste.
    assert "round1_sig_strike_share_r3_diff" in dataset.feature_names

    split = restrict_to_trainable_columns(temporal_split(dataset, test_fraction=0.25))

    assert "round1_sig_strike_share_r3_diff" not in split.x_train.columns
    assert "round1_sig_strike_share_r3_diff" not in split.x_test.columns
    assert list(split.x_train.columns) == list(split.x_test.columns) == ["reach_cm_diff"]
    # Sem a guarda, este ``fit`` quebra na coluna toda-NaN do treino; com ela, o treino roda.
    model = train_model(split.x_train, split.y_train, random_state=0)
    assert len(model.predict(split.x_test)) == len(split.y_test)


def test_restrict_to_trainable_columns_preserva_feature_com_dado_no_treino() -> None:
    """Coluna parcialmente preenchida **dentro do treino** é preservada nas duas fatias.

    A guarda descarta ausência total no treino, não ausência parcial: o classificador trata
    ``NaN`` nativamente e remover a coluna jogaria fora informação real.
    """
    dataset = build_dataset(_raw_com_feature_tardia(20, n_preenchidas=18))

    split = restrict_to_trainable_columns(temporal_split(dataset, test_fraction=0.25))

    assert "round1_sig_strike_share_r3_diff" in split.x_train.columns
    assert "round1_sig_strike_share_r3_diff" in split.x_test.columns


def test_restrict_to_trainable_columns_loga_as_colunas_descartadas(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """O descarte é explícito no log, com o nome da coluna e o corte temporal que a escondeu.

    Silêncio aqui recriaria o problema que a guarda existe para resolver: quem roda o treino
    precisa saber que features saíram do vetor e por quê.
    """
    dataset = build_dataset(_raw_com_feature_tardia(20, n_preenchidas=5))

    with caplog.at_level(logging.WARNING, logger="analysis.dataset"):
        restrict_to_trainable_columns(temporal_split(dataset, test_fraction=0.25))

    mensagem = caplog.text
    assert "round1_sig_strike_share_r3_diff" in mensagem
    assert "reach_cm_diff" not in mensagem


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


def test_build_dataset_nunca_admite_coluna_de_media_de_carreira_proscrita() -> None:
    """CA-11 da SPEC 007: nenhuma das 8 médias de carreira do CSV entra como feature.

    As colunas ``splm``/``str_acc``/``sapm``/``str_def``/``td_avg``/``td_avg_acc``/``td_def``/
    ``sub_avg`` do ``fighter_details.csv`` são um **snapshot de 2025**: a média de carreira de
    um lutador embute as lutas posteriores à que se quer prever (ADR 0002). Usá-las como
    preditor é vazamento de futuro, em qualquer passo do treino ou do walk-forward.

    A guarda vale para as três variantes da convenção do M4 (``_a``/``_b``/``_diff``) e para o
    nome nu; a coluna legítima ao lado permanece, provando que o filtro é do conjunto explícito
    e não um descarte heurístico por prefixo.
    """
    proscritas = {
        "splm_a": 4.1,
        "str_acc_b": 0.51,
        "td_avg_diff": -1.2,
        "sub_avg": 0.7,
    }
    raw = pd.DataFrame(
        [
            _raw_row(
                bout_id=1,
                event_date=date(2020, 1, 1),
                target="red",
                features={"reach_cm_diff": 5.0, **proscritas},
            ),
            _raw_row(
                bout_id=2,
                event_date=date(2020, 2, 1),
                target="blue",
                features={"reach_cm_diff": -3.0, **proscritas},
            ),
        ]
    )

    dataset = build_dataset(raw)

    for coluna in proscritas:
        assert coluna not in dataset.feature_names
        assert coluna not in dataset.features.columns
    assert dataset.feature_names == ["reach_cm_diff"]
