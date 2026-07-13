"""Teste do dry-run do matching em modo fixture -- Slice 04 (CA-05).

Contra o Postgres de teste (sessão transacional com rollback): semeia o evento persistido
correspondente à fixture ``event_stats_ufc-319.json`` e roda ``run_match_dry_run`` com o
``CitoClient`` em **modo fixture**. Assere cobertura 2/2 (100%), **0 quota real** (o custo é
cobrado no ``CallBudget`` -- exatamente 1 chamada por evento, nunca por-luta), e **0 escrita**
(nada em ``bout_fighter_rounds``; contagens de ``bouts``/``bout_fighters`` inalteradas).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito.client import DEFAULT_CALL_BUDGET, CallBudget, CitoClient
from ingestion.cito.matching import run_match_dry_run
from ingestion.normalize import normalize_name

_FIXTURES = Path(__file__).parent / "fixtures"


def _seed_fighter(session: Session, name: str) -> int:
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
    session.add(fighter)
    session.flush()
    return fighter.id


def _seed_ufc319(session: Session) -> Event:
    """Semeia o evento UFC 319 com uma luta e os dois cantos (dricus x khamzat)."""
    event = Event(
        name="UFC 319: Du Plessis vs. Chimaev",
        date=date(2025, 8, 16),
        location=None,
        source="kaggle",
    )
    session.add(event)
    session.flush()

    red_id = _seed_fighter(session, "Dricus du Plessis")
    blue_id = _seed_fighter(session, "Khamzat Chimaev")

    bout = Bout(
        event_id=event.id,
        winner_id=blue_id,
        method=BoutMethod.SUBMISSION,
        round=3,
        ending_time_seconds=None,
        weight_class="Middleweight",
        source="kaggle",
    )
    session.add(bout)
    session.flush()

    session.add_all(
        [
            BoutFighter(bout_id=bout.id, fighter_id=red_id, corner=Corner.RED, source="kaggle"),
            BoutFighter(bout_id=bout.id, fighter_id=blue_id, corner=Corner.BLUE, source="kaggle"),
        ]
    )
    session.flush()
    return event


def _fixture_client(budget: CallBudget) -> CitoClient:
    return CitoClient(
        token="", base_url="https://api.citoapi.com", fixture_dir=_FIXTURES, budget=budget
    )


def test_dry_run_reporta_cobertura_total(db_session: Session) -> None:
    """CA-05: o dry-run reporta cobertura 2/2 (100%) para o evento fixture UFC 319."""
    event = _seed_ufc319(db_session)
    budget = CallBudget(limit=DEFAULT_CALL_BUDGET)

    report = run_match_dry_run(db_session, event, _fixture_client(budget))

    assert report.matched == 2
    assert report.total == 2
    assert report.coverage == 1.0
    assert report.unmatched_slugs == ()


def test_dry_run_cobra_exatamente_uma_chamada(db_session: Session) -> None:
    """CA-05: exatamente 1 chamada por evento é cobrada no ``CallBudget`` (nunca por-luta)."""
    event = _seed_ufc319(db_session)
    budget = CallBudget(limit=DEFAULT_CALL_BUDGET)

    run_match_dry_run(db_session, event, _fixture_client(budget))

    assert budget.used == 1


def test_dry_run_nao_escreve_nada(db_session: Session) -> None:
    """CA-05: o dry-run é leitura pura -- 0 em ``bout_fighter_rounds``, contagens inalteradas."""
    event = _seed_ufc319(db_session)
    bouts_antes = db_session.scalar(select(func.count()).select_from(Bout))
    bout_fighters_antes = db_session.scalar(select(func.count()).select_from(BoutFighter))

    budget = CallBudget(limit=DEFAULT_CALL_BUDGET)
    run_match_dry_run(db_session, event, _fixture_client(budget))

    assert db_session.scalar(select(func.count()).select_from(Bout)) == bouts_antes
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == bout_fighters_antes
    assert db_session.scalar(select(func.count()).select_from(BoutFighterRound)) == 0


def test_dry_run_loga_cobertura(db_session: Session, caplog: pytest.LogCaptureFixture) -> None:
    """CA-05: a cobertura é emitida via ``logging`` (nunca ``print``)."""
    event = _seed_ufc319(db_session)
    budget = CallBudget(limit=DEFAULT_CALL_BUDGET)

    with caplog.at_level(logging.INFO, logger="ingestion.cito.matching"):
        run_match_dry_run(db_session, event, _fixture_client(budget))

    assert "2/2" in caplog.text
