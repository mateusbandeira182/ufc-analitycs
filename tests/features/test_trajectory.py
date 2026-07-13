"""Testes das features de trajetória de carreira e contexto físico -- Plano 005-03.

Cobrem as funções puras sobre DataFrame sintético (idade, layoff, experiência,
atributos físicos) e a integração CA-03 contra o Postgres de teste (lutador de
histórico conhecido semeado via ORM, lido de volta na mesma transação). A
corretude point-in-time -- layoff e experiência usam apenas lutas anteriores,
``NA``/``0`` na estreia -- é o requisito mais crítico desta slice.

As funções puras recebem frames já ordenados por ``(fighter_id, event_date,
bout_id)`` (contrato garantido pelo orquestrador ``add_trajectory_features``); os
testes as-of constroem a frame nessa ordem canônica. A integração reusa o estilo
de builders locais de ``tests/features/test_long_frame.py``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.fighters.enums import Stance
from apps.fighters.models import Fighter
from ingestion.features.cli import run_build
from ingestion.features.long_frame import build_long_frame, read_granular
from ingestion.features.trajectory import (
    AGE_YEARS,
    CAREER_BOUTS_BEFORE,
    HEIGHT_CM,
    LAYOFF_DAYS,
    REACH_CM,
    STANCE,
    TRAJECTORY_FEATURES,
    add_age,
    add_experience,
    add_layoff,
    add_physical_attributes,
    add_trajectory_features,
    load_fighters_bio,
    load_round_stats,
)
from ingestion.normalize import normalize_name

# --- Funções puras: idade -------------------------------------------------------------


def test_add_age_anos_completos_e_aniversario_nao_feito() -> None:
    """``age_years`` é a idade completa; aniversário ainda não feito no ano conta um a menos."""
    frame = pd.DataFrame(
        {
            "fighter_id": [1, 2, 3],
            "event_date": [date(2020, 6, 15), date(2020, 2, 1), date(2020, 3, 2)],
            "date_of_birth": [date(1990, 6, 15), date(1990, 3, 1), date(1990, 3, 2)],
        }
    )

    out = add_age(frame)

    # 2020-06-15 vs 1990-06-15: aniversário exatamente na data -> 30.
    # 2020-02-01 vs 1990-03-01: mês do evento antes do aniversário -> 29.
    # 2020-03-02 vs 1990-03-02: aniversário exatamente na data -> 30.
    assert out[AGE_YEARS].tolist() == [30, 29, 30]


def test_add_age_dob_ausente_vira_na() -> None:
    """Sem ``date_of_birth`` a idade é ``NA`` explícito (tipo nullable ``Int64``)."""
    frame = pd.DataFrame(
        {
            "fighter_id": [1],
            "event_date": [date(2020, 1, 1)],
            "date_of_birth": [None],
        }
    )

    out = add_age(frame)

    assert pd.isna(out[AGE_YEARS].iloc[0])
    assert out[AGE_YEARS].dtype == "Int64"


def test_add_age_nao_muta_a_frame_de_entrada() -> None:
    """``add_age`` opera sobre cópia -- a frame recebida não ganha a coluna."""
    frame = pd.DataFrame(
        {
            "fighter_id": [1],
            "event_date": [date(2020, 1, 1)],
            "date_of_birth": [date(1990, 1, 1)],
        }
    )

    add_age(frame)

    assert AGE_YEARS not in frame.columns


# --- Funções puras: layoff ------------------------------------------------------------


def test_add_layoff_dias_entre_consecutivas_e_na_na_estreia() -> None:
    """``layoff_days`` é a diferença em dias entre lutas consecutivas; ``NA`` na estreia."""
    d1, d2, d3 = date(2020, 1, 1), date(2020, 6, 1), date(2021, 1, 1)
    frame = pd.DataFrame(
        {
            "fighter_id": [1, 1, 1],
            "event_date": [d1, d2, d3],
            "bout_id": [10, 11, 12],
        }
    )

    out = add_layoff(frame)

    assert pd.isna(out[LAYOFF_DAYS].iloc[0])
    assert out[LAYOFF_DAYS].iloc[1] == (d2 - d1).days
    assert out[LAYOFF_DAYS].iloc[2] == (d3 - d2).days
    assert out[LAYOFF_DAYS].dtype == "Int64"


def test_add_layoff_sem_vazamento_entre_lutadores() -> None:
    """O layoff de um lutador não vaza para a estreia de outro (isolamento por grupo)."""
    frame = pd.DataFrame(
        {
            "fighter_id": [1, 1, 2, 2],
            "event_date": [date(2020, 1, 1), date(2020, 2, 1), date(2019, 1, 1), date(2019, 3, 1)],
            "bout_id": [10, 11, 20, 21],
        }
    )

    out = add_layoff(frame)

    # A primeira luta de cada lutador (índices 0 e 2) é estreia -> NA.
    assert pd.isna(out[LAYOFF_DAYS].iloc[0])
    assert pd.isna(out[LAYOFF_DAYS].iloc[2])
    assert out[LAYOFF_DAYS].iloc[1] == 31
    assert out[LAYOFF_DAYS].iloc[3] == 59


# --- Funções puras: experiência -------------------------------------------------------


def test_add_experience_contagem_cumulativa_zero_indexada() -> None:
    """``career_bouts_before`` é ``0,1,2,...`` -- o nº de lutas anteriores (exclui a corrente)."""
    frame = pd.DataFrame(
        {
            "fighter_id": [1, 1, 1, 1],
            "event_date": [date(2020, i, 1) for i in range(1, 5)],
            "bout_id": [10, 11, 12, 13],
        }
    )

    out = add_experience(frame)

    assert out[CAREER_BOUTS_BEFORE].tolist() == [0, 1, 2, 3]
    assert out[CAREER_BOUTS_BEFORE].dtype == "int64"


def test_add_experience_isolamento_por_fighter_id() -> None:
    """Dois lutadores intercalados mantêm contagens independentes (0,1,.. cada um)."""
    frame = pd.DataFrame(
        {
            "fighter_id": [1, 2, 1, 2, 1],
            "event_date": [
                date(2020, 1, 1),
                date(2020, 1, 2),
                date(2020, 2, 1),
                date(2020, 2, 2),
                date(2020, 3, 1),
            ],
            "bout_id": [10, 20, 11, 21, 12],
        }
    )

    out = add_experience(frame)

    assert out.loc[out["fighter_id"] == 1, CAREER_BOUTS_BEFORE].tolist() == [0, 1, 2]
    assert out.loc[out["fighter_id"] == 2, CAREER_BOUTS_BEFORE].tolist() == [0, 1]


# --- Funções puras: atributos físicos -------------------------------------------------


def _fighters_bio_frame() -> pd.DataFrame:
    """Bio de dois lutadores: um completo, um sem alcance (``reach_cm`` nulo)."""
    return pd.DataFrame(
        {
            "fighter_id": [1, 2],
            "date_of_birth": [date(1990, 1, 1), date(1985, 5, 5)],
            "height_cm": [168, 180],
            "reach_cm": [182, None],
            "stance": ["orthodox", "southpaw"],
        }
    )


def test_add_physical_attributes_anexa_bio_por_linha() -> None:
    """Cada linha recebe altura/alcance/base do ``fighter_id`` correspondente."""
    frame = pd.DataFrame({"fighter_id": [1, 2, 1], "bout_id": [10, 20, 11]})

    out = add_physical_attributes(frame, _fighters_bio_frame())

    assert out[HEIGHT_CM].tolist() == [168, 180, 168]
    assert out[STANCE].tolist() == ["orthodox", "southpaw", "orthodox"]
    assert out[HEIGHT_CM].dtype == "Int64"


def test_add_physical_attributes_reach_ausente_vira_na() -> None:
    """Lutador sem alcance recebe ``NA`` explícito (não zero, não vazio)."""
    frame = pd.DataFrame({"fighter_id": [2], "bout_id": [20]})

    out = add_physical_attributes(frame, _fighters_bio_frame())

    assert pd.isna(out[REACH_CM].iloc[0])
    assert out[REACH_CM].dtype == "Int64"


def test_add_physical_attributes_fighter_duplicado_falha_alto() -> None:
    """Bio com ``fighter_id`` duplicado falha (guarda de integridade da entity resolution)."""
    frame = pd.DataFrame({"fighter_id": [1], "bout_id": [10]})
    duplicado = pd.DataFrame(
        {
            "fighter_id": [1, 1],
            "date_of_birth": [date(1990, 1, 1), date(1990, 1, 1)],
            "height_cm": [168, 169],
            "reach_cm": [182, 183],
            "stance": ["orthodox", "southpaw"],
        }
    )

    with pytest.raises(pd.errors.MergeError):
        add_physical_attributes(frame, duplicado)


def test_add_physical_attributes_nao_muta_a_frame_de_entrada() -> None:
    """``add_physical_attributes`` não adiciona colunas à frame recebida."""
    frame = pd.DataFrame({"fighter_id": [1], "bout_id": [10]})

    add_physical_attributes(frame, _fighters_bio_frame())

    assert HEIGHT_CM not in frame.columns


# --- Orquestrador: reordenação defensiva ----------------------------------------------


def test_add_trajectory_features_reordena_defensivamente() -> None:
    """Frame fora de ordem é reordenada para a canônica antes de layoff/experiência."""
    long_frame = pd.DataFrame(
        {
            "fighter_id": [1, 1, 1],
            "event_date": [date(2021, 1, 1), date(2020, 1, 1), date(2020, 6, 1)],
            "bout_id": [12, 10, 11],
        }
    )
    fighters = pd.DataFrame(
        {
            "fighter_id": [1],
            "date_of_birth": [date(1990, 1, 1)],
            "height_cm": [180],
            "reach_cm": [185],
            "stance": ["orthodox"],
        }
    )

    out = add_trajectory_features(long_frame, fighters)

    # Reordenada por data: b10 (estreia), b11, b12.
    assert out["bout_id"].tolist() == [10, 11, 12]
    assert out[CAREER_BOUTS_BEFORE].tolist() == [0, 1, 2]
    assert pd.isna(out[LAYOFF_DAYS].iloc[0])
    assert set(TRAJECTORY_FEATURES).issubset(set(out.columns))
    # A coluna auxiliar de nascimento não sobra na saída (só as features de trajetória).
    assert "date_of_birth" not in out.columns


# --- Integração CA-03: lutador de histórico conhecido no Postgres ---------------------


def _add_fighter(
    session: Session,
    name: str,
    *,
    dob: date | None = None,
    height_cm: int | None = None,
    reach_cm: int | None = None,
    stance: Stance | None = None,
) -> Fighter:
    """Semeia um lutador com bio parametrizável e devolve o model já com id."""
    fighter = Fighter(
        name=name,
        name_normalized=normalize_name(name),
        nickname=None,
        date_of_birth=dob,
        height_cm=height_cm,
        reach_cm=reach_cm,
        stance=stance,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
    )
    session.add(fighter)
    session.flush()
    return fighter


def _add_event(session: Session, name: str, event_date: date) -> Event:
    """Semeia um evento de apoio e devolve o model já com id após o flush."""
    event = Event(name=name, date=event_date, location=None, source="kaggle")
    session.add(event)
    session.flush()
    return event


def _add_bout(session: Session, *, event: Event, red: Fighter, blue: Fighter) -> Bout:
    """Semeia uma luta com os dois cantos (red vence), stats irrelevantes para trajetória."""
    bout = Bout(
        event_id=event.id,
        winner_id=red.id,
        method=BoutMethod.DECISION,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source="kaggle",
    )
    session.add(bout)
    session.flush()
    session.add_all(
        [
            BoutFighter(
                bout_id=bout.id,
                fighter_id=red.id,
                corner=Corner.RED,
                knockdowns=None,
                sig_strikes_landed=None,
                sig_strikes_attempted=None,
                takedowns_landed=None,
                takedowns_attempted=None,
                submission_attempts=None,
                control_time_seconds=None,
                source="kaggle",
            ),
            BoutFighter(
                bout_id=bout.id,
                fighter_id=blue.id,
                corner=Corner.BLUE,
                knockdowns=None,
                sig_strikes_landed=None,
                sig_strikes_attempted=None,
                takedowns_landed=None,
                takedowns_attempted=None,
                submission_attempts=None,
                control_time_seconds=None,
                source="kaggle",
            ),
        ]
    )
    session.flush()
    return bout


def _seed_known_history(session: Session) -> tuple[int, list[date]]:
    """Semeia um lutador de histórico conhecido com três lutas em datas conhecidas.

    Volkanovski nasce em 1988-09-29 (altura 168, alcance 182, base ortodoxa) e luta em
    três eventos, cada um contra um adversário distinto. Devolve o ``fighter_id`` do
    lutador-alvo e as datas dos eventos em ordem cronológica.
    """
    hero = _add_fighter(
        session,
        "Alexander Volkanovski",
        dob=date(1988, 9, 29),
        height_cm=168,
        reach_cm=182,
        stance=Stance.ORTHODOX,
    )
    dates = [date(2016, 11, 26), date(2018, 6, 9), date(2019, 12, 14)]
    for i, event_date in enumerate(dates):
        rival = _add_fighter(session, f"Rival {i}")
        event = _add_event(session, f"UFC Known {i}", event_date)
        _add_bout(session, event=event, red=hero, blue=rival)
    return hero.id, dates


def test_trajectory_ca03_lutador_de_historico_conhecido(db_session: Session) -> None:
    """CA-03/CA-03.1/CA-03.2: idade, layoff, experiência e físico batem para o lutador conhecido."""
    hero_id, dates = _seed_known_history(db_session)
    long_frame = build_long_frame(read_granular(db_session))
    fighters = load_fighters_bio(db_session.connection())

    out = add_trajectory_features(long_frame, fighters)
    do_hero = out.loc[out["fighter_id"] == hero_id].sort_values("event_date").reset_index(drop=True)

    # Idade: nascimento 1988-09-29. 2016-11-26 -> 28; 2018-06-09 (antes do aniversário) -> 29;
    # 2019-12-14 -> 31.
    assert do_hero[AGE_YEARS].tolist() == [28, 29, 31]
    # Layoff: NA na estreia, diferença em dias entre eventos consecutivos.
    assert pd.isna(do_hero[LAYOFF_DAYS].iloc[0])
    assert do_hero[LAYOFF_DAYS].iloc[1] == (dates[1] - dates[0]).days
    assert do_hero[LAYOFF_DAYS].iloc[2] == (dates[2] - dates[1]).days
    # Experiência: 0 na estreia, cumulativa.
    assert do_hero[CAREER_BOUTS_BEFORE].tolist() == [0, 1, 2]
    # Atributos físicos vigentes em cada linha (CA-03.2).
    assert do_hero[HEIGHT_CM].tolist() == [168, 168, 168]
    assert do_hero[REACH_CM].tolist() == [182, 182, 182]
    assert do_hero[STANCE].tolist() == ["orthodox", "orthodox", "orthodox"]


def test_cli_stage_trajectory_produz_frame_enriquecida(db_session: Session) -> None:
    """CA-03.3: o estágio ``trajectory`` do CLI produz a frame longa com as colunas novas."""
    hero_id, _ = _seed_known_history(db_session)

    df = run_build(db_session, "trajectory")

    assert set(TRAJECTORY_FEATURES).issubset(set(df.columns))
    do_hero = df.loc[df["fighter_id"] == hero_id]
    assert do_hero[CAREER_BOUTS_BEFORE].tolist() == [0, 1, 2]


def test_cli_stage_trajectory_e_deterministico(db_session: Session) -> None:
    """CA-03.3: rodar o estágio ``trajectory`` duas vezes produz o mesmo resultado."""
    _seed_known_history(db_session)

    primeira = run_build(db_session, "trajectory")
    segunda = run_build(db_session, "trajectory")

    pd.testing.assert_frame_equal(primeira, segunda)


def test_load_fighters_bio_uma_linha_por_lutador(db_session: Session) -> None:
    """``load_fighters_bio`` devolve id + DOB + físico, uma linha por lutador semeado."""
    _seed_known_history(db_session)

    bio = load_fighters_bio(db_session.connection())

    # 1 herói + 3 rivais = 4 lutadores, sem duplicar.
    assert len(bio) == 4
    assert set(bio.columns) == {"fighter_id", "date_of_birth", "height_cm", "reach_cm", "stance"}
    assert bio["fighter_id"].is_unique


# --- load_round_stats: leitura do round-a-round por conexão ----------------------------


def _add_rounds(session: Session, bout_fighter_id: int, rounds: dict[int, int]) -> None:
    """Semeia linhas de ``bout_fighter_rounds`` (round -> sig_strikes_landed) para um canto."""
    session.add_all(
        [
            BoutFighterRound(
                bout_fighter_id=bout_fighter_id,
                round=rnd,
                knockdowns=None,
                sig_strikes_landed=sig,
                sig_strikes_attempted=None,
                takedowns_landed=None,
                takedowns_attempted=None,
                submission_attempts=None,
                control_time_seconds=None,
                total_strikes_landed=None,
                total_strikes_attempted=None,
                head_landed=None,
                head_attempted=None,
                body_landed=None,
                body_attempted=None,
                leg_landed=None,
                leg_attempted=None,
                distance_landed=None,
                distance_attempted=None,
                clinch_landed=None,
                clinch_attempted=None,
                ground_landed=None,
                ground_attempted=None,
                reversals=None,
                source="cito",
            )
            for rnd, sig in rounds.items()
        ]
    )
    session.flush()


def test_load_round_stats_le_bout_fighter_rounds_por_conexao(db_session: Session) -> None:
    """CA-02: ``load_round_stats`` lê ``bout_fighter_rounds`` (não-commitado) via conexão.

    Enxerga o estado semeado dentro da transação (como ``load_fighters_bio``), expondo
    ``bout_id``/``fighter_id``/``round`` mais a stat por round, via a junção com
    ``bout_fighters``.
    """
    hero = _add_fighter(db_session, "Alexander Volkanovski", dob=date(1988, 9, 29))
    rival = _add_fighter(db_session, "Rival R")
    event = _add_event(db_session, "UFC Rounds: Test", date(2020, 1, 1))
    bout = _add_bout(db_session, event=event, red=hero, blue=rival)
    hero_bf = db_session.execute(
        select(BoutFighter).where(BoutFighter.bout_id == bout.id, BoutFighter.fighter_id == hero.id)
    ).scalar_one()
    _add_rounds(db_session, hero_bf.id, {1: 12, 2: 8, 3: 5})

    rounds = load_round_stats(db_session.connection())

    assert set(rounds.columns) >= {"bout_id", "fighter_id", "round", "sig_strikes_landed"}
    do_hero = rounds.loc[rounds["fighter_id"] == hero.id].sort_values("round")
    assert do_hero["round"].tolist() == [1, 2, 3]
    assert do_hero["sig_strikes_landed"].tolist() == [12, 8, 5]
    assert set(do_hero["bout_id"]) == {bout.id}
