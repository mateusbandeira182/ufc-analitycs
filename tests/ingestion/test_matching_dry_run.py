"""Teste do dry-run do matching em modo fixture -- Slice 04 (CA-05).

Contra o Postgres de teste (sessão transacional com rollback): semeia o evento persistido
correspondente à fixture ``event_stats_ufc-319.json`` -- com o ``cito_slug`` que a sincronização
de catálogo (Sprint 007-03) teria gravado -- e roda ``run_match_dry_run`` com o ``CitoClient`` em
**modo fixture**. Assere cobertura 2/2 (100%), **0 quota real** (o custo é cobrado no
``CallBudget`` -- exatamente 1 chamada por evento, nunca por-luta), e **0 escrita** (nada em
``bout_fighter_rounds``; contagens de ``bouts``/``bout_fighters`` inalteradas).

Cobre também o caminho catálogo-driven do CLI: ``_find_event_by_cito_slug`` localiza o evento
pela coluna persistida, e um evento sem ``cito_slug`` erra claro em vez de tentar adivinhar.
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
from ingestion.cito.matching import (
    BoutFighterMatchError,
    _find_event_by_cito_slug,
    run_match_dry_run,
)
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


def _seed_ufc319(session: Session, *, cito_slug: str | None = "ufc-319") -> Event:
    """Semeia o evento UFC 319 com uma luta e os dois cantos (dricus x khamzat).

    O ``cito_slug`` default é o identificador que a sincronização de catálogo persistiria;
    ``None`` reproduz um evento que o catálogo não casou.
    """
    event = Event(
        name="UFC 319: Du Plessis vs. Chimaev",
        date=date(2025, 8, 16),
        location=None,
        source="kaggle",
        cito_slug=cito_slug,
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


def test_find_event_by_cito_slug_localiza_pelo_identificador_persistido(
    db_session: Session,
) -> None:
    """CA-05: o CLI acha o evento pela coluna ``cito_slug``, sem derivar nada do nome."""
    event = _seed_ufc319(db_session)

    assert _find_event_by_cito_slug(db_session, "ufc-319").id == event.id


def test_find_event_by_cito_slug_sem_catalogo_sincronizado_erra_claro(
    db_session: Session,
) -> None:
    """CA-05: sem ``cito_slug`` persistido, a busca erra claro apontando a sincronização.

    Nunca cai para uma derivação por nome: sem identificador do catálogo, não há evento a casar.
    """
    _seed_ufc319(db_session, cito_slug=None)

    with pytest.raises(BoutFighterMatchError, match="catálogo"):
        _find_event_by_cito_slug(db_session, "ufc-319")


def test_dry_run_evento_sem_cito_slug_erra_claro(db_session: Session) -> None:
    """CA-05: o dry-run de um evento sem identificador do catálogo falha alto, sem gastar quota."""
    event = _seed_ufc319(db_session, cito_slug=None)
    budget = CallBudget(limit=DEFAULT_CALL_BUDGET)

    with pytest.raises(BoutFighterMatchError, match="cito_slug"):
        run_match_dry_run(db_session, event, _fixture_client(budget))

    assert budget.used == 0
