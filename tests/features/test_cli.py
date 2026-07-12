"""Testes do CLI de features -- ``run_build`` (dispatch por estágio) -- CA-01 do Plano 005-01.

Exercitam o entrypoint testável: o estágio ``long`` devolve a frame longa (mesma
contagem de participações) e um estágio desconhecido levanta ``ValueError`` claro. O
``main`` fino (abre ``SessionLocal``, não commita) é verificado por execução manual no
DoD da sprint, como ``ingestion/seed.py::main``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.features.cli import run_build
from ingestion.features.rolling import RECENT_FORM_FEATURES, WIN_RATE_PRIOR
from ingestion.normalize import normalize_name


def _seed_one_bout(db_session: Session) -> None:
    """Semeia uma luta com dois cantos, suficiente para a contagem do CLI."""
    fighters = []
    for name in ("Alexander Volkanovski", "Ilia Topuria"):
        fighter = Fighter(
            name=name,
            name_normalized=normalize_name(name),
            nickname=None,
            date_of_birth=None,
            height_cm=None,
            reach_cm=None,
            stance=None,
            wins=0,
            losses=0,
            draws=0,
            source="kaggle",
        )
        db_session.add(fighter)
        fighters.append(fighter)
    event = Event(name="UFC 300: Test", date=date(2024, 4, 13), location=None, source="kaggle")
    db_session.add(event)
    db_session.flush()
    bout = Bout(
        event_id=event.id,
        winner_id=fighters[1].id,
        method=BoutMethod.KO_TKO,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source="kaggle",
    )
    db_session.add(bout)
    db_session.flush()
    db_session.add_all(
        [
            BoutFighter(
                bout_id=bout.id,
                fighter_id=fighters[0].id,
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
                fighter_id=fighters[1].id,
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
    db_session.flush()


def test_run_build_long_devolve_a_frame(db_session: Session) -> None:
    """CA-01: ``run_build(stage="long")`` devolve a frame longa (2 linhas para 1 bout)."""
    _seed_one_bout(db_session)

    df = run_build(db_session, stage="long")

    assert len(df) == 2
    assert list(df["result"]) == ["loss", "win"]


def test_run_build_estagio_desconhecido_levanta_value_error(db_session: Session) -> None:
    """CA-01: um estágio não suportado levanta ``ValueError`` claro, sem tocar o banco."""
    with pytest.raises(ValueError, match="Estágio desconhecido"):
        run_build(db_session, stage="inexistente")


def _seed_two_bouts_same_fighter(db_session: Session) -> None:
    """Semeia duas lutas de um mesmo lutador (A) para exercitar o estágio ``rolling``.

    A vence a 1a (KO/TKO) e a 2a (decisão), contra oponentes distintos, com ``round`` e
    ``ending_time_seconds`` preenchidos para que as taxas por minuto sejam calculáveis.
    """
    names = ("Fighter A", "Opponent B", "Opponent C")
    a, b, c = (
        Fighter(
            name=name,
            name_normalized=normalize_name(name),
            nickname=None,
            date_of_birth=None,
            height_cm=None,
            reach_cm=None,
            stance=None,
            wins=0,
            losses=0,
            draws=0,
            source="kaggle",
        )
        for name in names
    )
    db_session.add_all([a, b, c])
    evt1 = Event(name="UFC 1: Test", date=date(2023, 1, 1), location=None, source="kaggle")
    evt2 = Event(name="UFC 2: Test", date=date(2023, 6, 1), location=None, source="kaggle")
    db_session.add_all([evt1, evt2])
    db_session.flush()

    def _add(event: Event, opponent: Fighter, method: BoutMethod) -> None:
        bout = Bout(
            event_id=event.id,
            winner_id=a.id,
            method=method,
            round=3,
            ending_time_seconds=300,
            weight_class=None,
            source="kaggle",
        )
        db_session.add(bout)
        db_session.flush()
        db_session.add_all(
            [
                BoutFighter(
                    bout_id=bout.id,
                    fighter_id=a.id,
                    corner=Corner.RED,
                    knockdowns=None,
                    sig_strikes_landed=30,
                    sig_strikes_attempted=None,
                    takedowns_landed=1,
                    takedowns_attempted=2,
                    submission_attempts=None,
                    control_time_seconds=60,
                    source="kaggle",
                ),
                BoutFighter(
                    bout_id=bout.id,
                    fighter_id=opponent.id,
                    corner=Corner.BLUE,
                    knockdowns=None,
                    sig_strikes_landed=10,
                    sig_strikes_attempted=None,
                    takedowns_landed=0,
                    takedowns_attempted=1,
                    submission_attempts=None,
                    control_time_seconds=0,
                    source="kaggle",
                ),
            ]
        )
        db_session.flush()

    _add(evt1, b, BoutMethod.KO_TKO)
    _add(evt2, c, BoutMethod.DECISION)


def test_run_build_rolling_enriquece_com_features_de_forma_recente(db_session: Session) -> None:
    """CA-05: ``run_build(stage="rolling")`` compõe a frame longa com as features de forma."""
    _seed_two_bouts_same_fighter(db_session)

    df = run_build(db_session, stage="rolling")

    assert set(RECENT_FORM_FEATURES).issubset(set(df.columns))
    # A 2a luta de A tem 1 luta anterior -> win rate acumulado definido (não NaN).
    fighter_a = df.loc[df["fighter_name"] == "Fighter A"].sort_values("event_date")
    assert fighter_a[WIN_RATE_PRIOR].iloc[1] == 1.0
    # Estreia (1a luta de A) -> NaN explícito.
    assert fighter_a[WIN_RATE_PRIOR].iloc[0] != fighter_a[WIN_RATE_PRIOR].iloc[0]


def test_run_build_rolling_e_deterministico(db_session: Session) -> None:
    """CA-05: rodar o estágio ``rolling`` duas vezes produz frames idênticas."""
    _seed_two_bouts_same_fighter(db_session)

    primeira = run_build(db_session, stage="rolling")
    segunda = run_build(db_session, stage="rolling")

    pd.testing.assert_frame_equal(primeira, segunda)
